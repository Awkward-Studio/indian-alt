import json
import logging
import time
from typing import Dict, Any, Optional, Iterator

from ..models import AIPersonality, AISkill, AIAuditLog
from .llm_providers import VLLMProviderService
from .prompts import PromptBuilderService
from .parsers import ResponseParserService
from .ocr import OCRService
from .realtime import broadcast_audit_log_update, log_worker_event
from .runtime import AIRuntimeService

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

logger = logging.getLogger(__name__)

class AIProcessorService:
    """
    Facade Orchestrator that coordinates:
    1. OCR passes via `OCRService`
        2. Prompt building via `PromptBuilderService`
        3. LLM API execution via `VLLMProviderService`
    4. Streaming and Response Parsing via `ResponseParserService`
    """

    def __init__(self):
        self.provider = VLLMProviderService()
        self.ocr_service = OCRService()
        self.available_models = self.provider.get_available_models()
        self.channel_layer = get_channel_layer()

    def process_content(
        self,
        content: str,
        personality_name: str = "default",
        skill_name: str = None,
        metadata: Optional[Dict[str, Any]] = None,
        source_id: Optional[str] = None,
        source_type: str = "email",
        images: Optional[list] = None,
        model_override: Optional[str] = None,
        stream: bool = False
    ) -> Any:
        
        if skill_name:
            print(f"[AI-PROCESSOR] Loading skill: {skill_name}")
            
        personality = AIRuntimeService.get_personality(personality_name)
        skill = AIRuntimeService.get_skill(skill_name)
        vision_model = AIRuntimeService.get_vision_model(personality)

        # Audit Log Setup (Internal bookkeeping) - Initialize early to avoid UnboundLocalError in log_worker_event
        audit_log = self._setup_audit_log(
            source_type, source_id, personality, skill, 
            "", "", metadata
        )

        # PHASE 1: OCR (Optional, delegated to OCRService)
        if images and skill_name == "deal_extraction":
            log_worker_event(audit_log, f"Starting OCR analysis for {len(images)} images.")
            ocr_context = self.ocr_service.transcribe(images, model=vision_model)
            content = f"{content}\n\n[HIGH-FIDELITY DOCUMENT OCR]:\n{ocr_context}"
            log_worker_event(audit_log, "OCR analysis complete.")

        # PHASE 2: REASONING SETUP (Delegated to PromptBuilderService)
        log_worker_event(audit_log, f"Preparing prompt for {AIRuntimeService.get_text_model(personality)}.")
        system_instructions = PromptBuilderService.build_system_instructions(personality, skill, stream)
        prompt_template = skill.prompt_template if skill else "{{ content }}"
        
        user_prompt, cleaned_text = PromptBuilderService.build_user_prompt(prompt_template, content, metadata)

        # Update Audit Log with the generated prompts
        audit_log.system_prompt = system_instructions
        audit_log.user_prompt = user_prompt
        audit_log.save(update_fields=['system_prompt', 'user_prompt'])
        
        log_worker_event(audit_log, "Sending request to AI model server.")

        payload = {
            "model": model_override or AIRuntimeService.get_text_model(personality),
            "prompt": user_prompt,
            "system": system_instructions,
            "stream": stream,
            "options": {
                "max_tokens": 8192,
                "temperature": 0.1,
            }
        }

        # PHASE 3: EXECUTION (Delegated to Provider + Parser)
        if stream:
            return self._stream_response(payload, audit_log)
        
        result = self._standard_response(payload, audit_log)
        result["_full_context"] = cleaned_text
        return result

    def _setup_audit_log(self, source_type, source_id, personality, skill, system_prompt, user_prompt, metadata) -> AIAuditLog:
        audit_log_id = metadata.get('audit_log_id') if metadata else None
        source_meta = metadata.get('_source_metadata') if metadata else None
        celery_task_id = metadata.get('celery_task_id') if metadata else None
        ctx_label = metadata.get('context_label') if metadata else None
        
        if audit_log_id:
            try:
                audit_log = AIAuditLog.objects.get(id=audit_log_id)
                audit_log.system_prompt = system_prompt
                audit_log.user_prompt = user_prompt
                audit_log.status = 'PROCESSING'
                if source_meta: audit_log.source_metadata = source_meta
                if celery_task_id: audit_log.celery_task_id = celery_task_id
                if ctx_label: audit_log.context_label = ctx_label
                audit_log.save()
                broadcast_audit_log_update(audit_log, event_type="snapshot", done=False)
                return audit_log
            except AIAuditLog.DoesNotExist:
                pass

        return AIRuntimeService.create_audit_log(
            source_type=source_type,
            source_id=source_id,
            context_label=ctx_label,
            personality=personality,
            skill=skill,
            model_used=AIRuntimeService.get_text_model(personality),
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            is_success=False,
            status='PROCESSING',
            source_metadata=source_meta,
            celery_task_id=celery_task_id,
        )

    def _stream_response(self, payload: dict, audit_log: AIAuditLog) -> Iterator[str]:
        """
        Orchestrates streaming execution and robust parsing.
        Broadcasts each chunk via WebSockets and calculates metrics.
        """
        room_name = f'ai_stream_{str(audit_log.id)}'
        start_time = time.time()
        
        try:
            full_response = ""
            full_thinking = ""
            chunk_counter = 0

            stream_iterator = self.provider.execute_stream(payload)
            
            for ui_chunk, thinking_delta, response_delta in ResponseParserService.parse_stream(stream_iterator):
                full_thinking += thinking_delta
                full_response += response_delta
                
                # Broadcast to WebSockets
                if self.channel_layer:
                    async_to_sync(self.channel_layer.group_send)(
                        room_name,
                        {
                            "type": "ai_message",
                            "event_type": "delta",
                            "audit_log_id": str(audit_log.id),
                            "response": response_delta,
                            "thinking": thinking_delta,
                            "response_delta": response_delta,
                            "thinking_delta": thinking_delta,
                            "status": "processing",
                            "done": False
                        }
                    )
                
                yield json.dumps(ui_chunk) + "\n"

                # Throttle DB saves to reduce contention
                chunk_counter += 1
                if chunk_counter % 20 == 0:
                    audit_log.raw_response = full_response
                    audit_log.raw_thinking = full_thinking
                    audit_log.save(update_fields=['raw_response', 'raw_thinking'])

            # Finalize metrics
            duration_ms = int((time.time() - start_time) * 1000)
            # Estimate tokens: ~4 chars per token for average English text
            estimated_tokens = (len(full_response) + len(full_thinking) + len(audit_log.user_prompt or "")) // 4

            # Finalize audit log
            audit_log.raw_response = full_response
            audit_log.raw_thinking = full_thinking
            audit_log.is_success = True
            audit_log.status = 'COMPLETED'
            audit_log.request_duration_ms = duration_ms
            audit_log.tokens_used = estimated_tokens
            audit_log.save()
            broadcast_audit_log_update(audit_log, event_type="terminal", done=True)
            
            # Broadcast final completion
            if self.channel_layer:
                async_to_sync(self.channel_layer.group_send)(
                    room_name,
                    {
                        "type": "ai_message",
                        "event_type": "terminal",
                        "audit_log_id": str(audit_log.id),
                        "response": "",
                        "thinking": "",
                        "status": "completed",
                        "done": True
                    }
                )
            
        except Exception as e:
            logger.error(f"Streaming failed: {str(e)}")
            audit_log.is_success = False
            audit_log.status = 'FAILED'
            audit_log.error_message = str(e)
            audit_log.request_duration_ms = int((time.time() - start_time) * 1000)
            audit_log.save()
            broadcast_audit_log_update(audit_log, event_type="terminal", done=True)
            
            if self.channel_layer:
                async_to_sync(self.channel_layer.group_send)(
                    room_name,
                    {
                        "type": "ai_message",
                        "event_type": "terminal",
                        "audit_log_id": str(audit_log.id),
                        "response": f"Error: {str(e)}",
                        "thinking": "",
                        "status": "failed",
                        "done": True
                    }
                )
            
            yield json.dumps({"response": f"Error: {str(e)}", "done": True})

    def _standard_response(self, payload: dict, audit_log: AIAuditLog) -> Dict[str, Any]:
        """
        Orchestrates standard execution and delegates parsing.
        """
        start_time = time.time()
        try:
            data = self.provider.execute_standard(payload)
            
            raw_response = data.get("response") or data.get("thinking", "")
            thinking = data.get("thinking", "")
            
            extraction_skills = {"deal_extraction", "document_evidence_extraction", "deal_synthesis", "vdr_incremental_analysis"}
            is_extraction = audit_log.skill and audit_log.skill.name in extraction_skills
            
            parsed_json, success, clean_resp, clean_think = ResponseParserService.parse_standard_response(
                raw_response, thinking, is_extraction
            )

            audit_log.raw_response = clean_resp
            audit_log.raw_thinking = clean_think
            
            if success:
                audit_log.parsed_json = parsed_json
                audit_log.is_success = True
                audit_log.status = 'COMPLETED'
            else:
                if is_extraction and isinstance(parsed_json, dict) and parsed_json.get("_salvaged"):
                    audit_log.parsed_json = parsed_json
                    audit_log.is_success = True
                    audit_log.status = 'COMPLETED'
                    audit_log.error_message = None
                    logger.warning(
                        "AuditLog %s completed with salvaged extraction payload.",
                        audit_log.id,
                    )
                else:
                    audit_log.is_success = False
                    audit_log.status = 'FAILED'
                    audit_log.error_message = parsed_json.get('error', 'AI response was truncated or malformed (JSON block not found).')
                    logger.error(f"AuditLog {audit_log.id} failed parsing: {audit_log.error_message}")
                
            # Estimate tokens
            usage = data.get("usage") or {}
            audit_log.tokens_used = usage.get("total_tokens") or (len(clean_resp) + len(clean_think) + len(audit_log.user_prompt or "")) // 4
                
        except Exception as e:
            logger.error(f"Standard execution failed: {str(e)}")
            audit_log.is_success = False
            audit_log.status = 'FAILED'
            audit_log.error_message = str(e)
            parsed_json = {"error": str(e)}
        finally:
            audit_log.request_duration_ms = int((time.time() - start_time) * 1000)
            audit_log.save()
            broadcast_audit_log_update(
                audit_log,
                event_type="terminal" if audit_log.status in ['COMPLETED', 'FAILED'] else "snapshot",
                done=audit_log.status in ['COMPLETED', 'FAILED'],
            )
            
        return parsed_json
