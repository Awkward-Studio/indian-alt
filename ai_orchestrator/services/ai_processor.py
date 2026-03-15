import json
import logging
import requests
import time
from typing import Dict, Any, Optional, List
from django.conf import settings
from bs4 import BeautifulSoup
import re

from ..models import AIPersonality, AISkill, AIAuditLog

logger = logging.getLogger(__name__)

class AIProcessorService:
    """
    Orchestrated Forensic Pipeline:
    1. OCR PASS: GLM-OCR transcribes all images/PDF pages into high-fidelity text.
    2. REASONING PASS: Qwen 3.5 analyzes combined Email + OCR text for deal signals.
    3. Persists full context for RAG indexing.
    """

    def __init__(self):
        self.ollama_url = getattr(settings, 'OLLAMA_URL', 'http://52.172.249.12:11434')
        self.available_models = self.get_available_models()
        self.text_priority = ['qwen3.5:latest']
        self.vision_priority = ['qwen3.5:latest']

    def get_available_models(self) -> List[str]:
        try:
            response = requests.get(f"{self.ollama_url}/api/tags", timeout=5)
            response.raise_for_status()
            data = response.json()
            return [model['name'] for model in data.get('models', [])]
        except Exception as e:
            logger.error(f"Error fetching Ollama models: {str(e)}")
            return []

    def clean_html(self, html_content: str) -> str:
        if not html_content: return ""
        soup = BeautifulSoup(html_content, "html.parser")
        for script_or_style in soup(["script", "style"]):
            script_or_style.decompose()
        text = soup.get_text()
        text = re.sub(r'\n\s*\n', '\n\n', text)
        text = re.sub(r' +', ' ', text)
        return text.strip()

    def _extract_json(self, text: str) -> str:
        """Robustly find and extract JSON string from a larger text block."""
        try:
            # 1. Try common tags/blocks first
            patterns = [
                r'<json>\s*(\{.*?\})\s*</json>',
                r'```json\s*(\{.*?\})\s*```',
                r'```\s*(\{.*?\})\s*```'
            ]
            for pattern in patterns:
                match = re.search(pattern, text, re.DOTALL)
                if match:
                    return match.group(1).strip()

            # 2. Fallback to raw brace matching
            first_brace = text.find('{')
            last_brace = text.rfind('}')
            if first_brace != -1 and last_brace != -1:
                return text[first_brace:last_brace + 1]
        except:
            pass
        return text

    def ocr_transcribe(self, images: list) -> str:
        """Step 1: Specialized OCR using GLM-OCR specialist model."""
        if not images: return ""
        print(f"[AI-PIPELINE] Phase 1: Transcribing {len(images)} document pages via GLM-OCR...")
        transcription = ""
        for i, img in enumerate(images):
            print(f"    [AI-PIPELINE] Phase 1: Transcribing page {i+1} of {len(images)} via GLM-OCR...")
            payload = {
                "model": "glm-ocr:latest",
                "prompt": "Extract all text and tabular data from this document page exactly. Output Markdown.",
                "images": [img],
                "stream": False,
                "keep_alive": "2h"
            }
            try:
                resp = requests.post(f"{self.ollama_url}/api/generate", json=payload, timeout=120)
                if resp.status_code == 200:
                    page_text = resp.json().get("response", "")
                    transcription += f"\n\n--- PDF PAGE {i+1} TRANSCRIPTION ---\n{page_text}"
            except Exception as e:
                logger.error(f"OCR Phase failed on page {i+1}: {str(e)}")
        return transcription

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
        """
        Orchestrates the multi-model forensic analysis.
        """
        if skill_name:
            print(f"[AI-PROCESSOR] Loading skill: {skill_name}")
        # PHASE 1: OCR (if images exist)
        ocr_context = ""
        if images and skill_name == "deal_extraction":
            ocr_context = self.ocr_transcribe(images)
            # We add this high-fidelity transcription to our prompt context
            content = f"{content}\n\n[HIGH-FIDELITY DOCUMENT OCR]:\n{ocr_context}"
            # Images are cleared to prevent double-processing and token bloat in Qwen
            images = None 

        # PHASE 2: REASONING (Qwen 3.5)
        print(f"[AI-PIPELINE] Phase 2: Orchestrating Forensic Logic with Qwen 3.5...")
        
        try:
            if personality_name == "default":
                personality = AIPersonality.objects.get(is_default=True)
            else:
                personality = AIPersonality.objects.get(name=personality_name)
        except: personality = None

        try:
            skill = AISkill.objects.get(name=skill_name) if skill_name else None
        except: skill = None

        cleaned_text = self.clean_html(content)
        max_chars = 160000 
        if len(cleaned_text) > max_chars:
            cleaned_text = cleaned_text[:100000] + "\n\n[... TRUNCATED ...]\n\n" + cleaned_text[-60000:]

        system_instructions = personality.system_instructions if personality else "You are a PE analyst."
        
        # If a skill is active, we should prefix the system instructions with the skill's specific intent
        # to ensure it doesn't get lost in the personality's base instructions.
        if skill:
            skill_identity = f"# CURRENT TASK: {skill.name.upper().replace('_', ' ')}\n{skill.description}\n\n"
            system_instructions = skill_identity + system_instructions
        
        # --- DYNAMIC PROTOCOL INJECTION ---
        from ..models import AnalysisProtocol
        from .forex_service import ForexService
        
        protocol = AnalysisProtocol.objects.filter(is_active=True).first()
        if protocol and protocol.directives:
            forex = ForexService()
            live_rate = forex.get_crore_string()
            
            directives_text = "\n### INSTITUTIONAL ANALYSIS DIRECTIVES:\n"
            for d in protocol.directives:
                # If the directive mentions currency, append the live rate
                if any(word in d.lower() for word in ['currency', 'inr', 'crore', '$']):
                    directives_text += f"- {d} (CURRENT LIVE RATE: 1M USD = {live_rate})\n"
                else:
                    directives_text += f"- {d}\n"
            system_instructions += directives_text
        # ----------------------------------

        if not stream:
            system_instructions += "\n\nIMPORTANT: Return ONLY a valid JSON object. Do not include any thinking text in the final response."
        
        prompt_template = skill.prompt_template if skill else "{{ content }}"
        user_prompt = prompt_template
        
        # 1. First replace specific metadata keys (like context_data, history_context)
        if metadata:
            for key, value in metadata.items():
                val_str = str(value)
                # Handle various bracket styles: {{key}}, {{ key }}, {{  key  }}
                user_prompt = user_prompt.replace('{{' + key + '}}', val_str)
                user_prompt = user_prompt.replace('{{ ' + key + ' }}', val_str)
                user_prompt = user_prompt.replace('{{  ' + key + '  }}', val_str)
                user_prompt = user_prompt.replace('{{' + key.lower() + '}}', val_str)
                user_prompt = user_prompt.replace('{{ ' + key.lower() + ' }}', val_str)
        
        # 2. Then replace the primary content placeholder
        user_prompt = user_prompt.replace('{{content}}', cleaned_text)
        user_prompt = user_prompt.replace('{{ content }}', cleaned_text)
        user_prompt = user_prompt.replace('{{  content  }}', cleaned_text)
        
        # 3. Safety Fallback: If user message is still not in the prompt, append it
        if cleaned_text not in user_prompt:
            user_prompt = f"{user_prompt}\n\n[USER INQUIRY]:\n{cleaned_text}"

        # VERIFICATION LOG: Ensure data is actually in the prompt
        if metadata and "context_data" in metadata:
            if "{{ context_data }}" in user_prompt:
                logger.error(f"[AI-PROCESSOR] CRITICAL: Template replacement FAILED for '{{{{ context_data }}}}'.")
            else:
                logger.info(f"[AI-PROCESSOR] Template replacement SUCCESS. Context injected.")

        audit_log_id = metadata.get('audit_log_id') if metadata else None
        source_meta = metadata.get('_source_metadata') if metadata else None
        celery_task_id = metadata.get('celery_task_id') if metadata else None
        ctx_label = metadata.get('context_label') if metadata else None
        
        audit_log = None
        if audit_log_id:
            try:
                audit_log = AIAuditLog.objects.get(id=audit_log_id)
                audit_log.system_prompt = system_instructions
                audit_log.user_prompt = user_prompt
                audit_log.status = 'PROCESSING'
                if source_meta:
                    audit_log.source_metadata = source_meta
                if celery_task_id:
                    audit_log.celery_task_id = celery_task_id
                if ctx_label:
                    audit_log.context_label = ctx_label
                audit_log.save()
            except AIAuditLog.DoesNotExist:
                pass

        if not audit_log:
            audit_log = AIAuditLog.objects.create(
                source_type=source_type, source_id=source_id,
                context_label=ctx_label,
                personality=personality, skill=skill,
                model_used='qwen3.5:latest', system_prompt=system_instructions, user_prompt=user_prompt,
                is_success=False, status='PROCESSING',
                source_metadata=source_meta,
                celery_task_id=celery_task_id
            )

        payload = {
            "model": 'qwen3.5:latest',
            "prompt": user_prompt,
            "system": system_instructions,
            "stream": stream,
            "keep_alive": "2h",
            "options": {
                "num_ctx": 32768,
                "temperature": 0.1,
                "num_gpu": 99
            }
        }

        if stream:
            return self._stream_response(payload, audit_log)
        
        result = self._standard_response(payload, audit_log)
        # We attach the full combined context (Email + OCR) to the result 
        # so the backend view can save it for RAG
        result["_full_context"] = cleaned_text
        return result

    def _stream_response(self, payload: dict, audit_log: AIAuditLog):
        try:
            response = requests.post(f"{self.ollama_url}/api/generate", json=payload, stream=True, timeout=300)
            response.raise_for_status()
            
            full_response = ""
            full_thinking = ""
            is_thinking_mode = False
            is_response_mode = False
            
            # Buffer for handling split tags
            buffer = ""
            
            chunk_counter = 0
            for line in response.iter_lines():
                if line:
                    chunk = json.loads(line)
                    raw_text = chunk.get("response") or ""
                    buffer += raw_text
                    
                    # Process buffer and yield what we can
                    while True:
                        if is_thinking_mode:
                            if "</thinking>" in buffer:
                                parts = buffer.split("</thinking>", 1)
                                content = parts[0]
                                yield json.dumps({"response": "", "thinking": content, "done": False}) + "\n"
                                full_thinking += content
                                buffer = parts[1]
                                is_thinking_mode = False
                            else:
                                # Avoid yielding partial "</thinking>"
                                if "</" in buffer:
                                    tag_start = buffer.find("</")
                                    to_yield = buffer[:tag_start]
                                    if to_yield:
                                        yield json.dumps({"response": "", "thinking": to_yield, "done": False}) + "\n"
                                        full_thinking += to_yield
                                        buffer = buffer[tag_start:]
                                    break
                                else:
                                    if buffer:
                                        yield json.dumps({"response": "", "thinking": buffer, "done": False}) + "\n"
                                        full_thinking += buffer
                                        buffer = ""
                                    break
                        elif is_response_mode:
                            if "</response>" in buffer:
                                parts = buffer.split("</response>", 1)
                                content = parts[0]
                                yield json.dumps({"response": content, "thinking": "", "done": False}) + "\n"
                                full_response += content
                                buffer = parts[1]
                                is_response_mode = False
                            else:
                                if "</" in buffer:
                                    tag_start = buffer.find("</")
                                    to_yield = buffer[:tag_start]
                                    if to_yield:
                                        yield json.dumps({"response": to_yield, "thinking": "", "done": False}) + "\n"
                                        full_response += to_yield
                                        buffer = buffer[tag_start:]
                                    break
                                else:
                                    if buffer:
                                        yield json.dumps({"response": buffer, "thinking": "", "done": False}) + "\n"
                                        full_response += buffer
                                        buffer = ""
                                    break
                        else:
                            # Not in any mode, look for start tags
                            if "<thinking>" in buffer:
                                parts = buffer.split("<thinking>", 1)
                                if parts[0]:
                                    yield json.dumps({"response": parts[0], "thinking": "", "done": False}) + "\n"
                                    full_response += parts[0]
                                buffer = parts[1]
                                is_thinking_mode = True
                            elif "<response>" in buffer:
                                parts = buffer.split("<response>", 1)
                                if parts[0]:
                                    yield json.dumps({"response": parts[0], "thinking": "", "done": False}) + "\n"
                                    full_response += parts[0]
                                buffer = parts[1]
                                is_response_mode = True
                            elif "<" in buffer:
                                # Wait for full tag
                                tag_start = buffer.find("<")
                                to_yield = buffer[:tag_start]
                                if to_yield:
                                    yield json.dumps({"response": to_yield, "thinking": "", "done": False}) + "\n"
                                    full_response += to_yield
                                    buffer = buffer[tag_start:]
                                break
                            else:
                                if buffer:
                                    yield json.dumps({"response": buffer, "thinking": "", "done": False}) + "\n"
                                    full_response += buffer
                                    buffer = ""
                                break

                    # Persist to DB periodically
                    chunk_counter += 1
                    if chunk_counter % 10 == 0:
                        audit_log.raw_response = full_response
                        audit_log.raw_thinking = full_thinking
                        audit_log.save(update_fields=['raw_response', 'raw_thinking'])

                    if chunk.get("done"): break
            
            # Final buffer flush
            if buffer:
                # Any remaining text is treated as response
                yield json.dumps({"response": buffer, "thinking": "", "done": False}) + "\n"
                full_response += buffer

            audit_log.raw_response = full_response
            audit_log.raw_thinking = full_thinking
            audit_log.is_success = True
            audit_log.status = 'COMPLETED'
            audit_log.save()
        except Exception as e:
            audit_log.is_success = False
            audit_log.status = 'FAILED'
            audit_log.error_message = str(e)
            audit_log.save()
            yield json.dumps({"response": f"Error: {str(e)}", "done": True})

    def _standard_response(self, payload: dict, audit_log: AIAuditLog) -> Dict[str, Any]:
        start_time = time.time()
        try:
            response = requests.post(f"{self.ollama_url}/api/generate", json=payload, timeout=2400)
            response.raise_for_status()
            data = response.json()
            
            raw_response = data.get("response") or data.get("thinking", "")
            thinking = data.get("thinking", "")
            
            # If thinking is empty but raw_response has tags, extract them
            if not thinking and "<thinking>" in raw_response:
                t_match = re.search(r'<thinking>(.*?)</thinking>', raw_response, re.DOTALL)
                if t_match:
                    thinking = t_match.group(1).strip()
            
            # Clean the main response of any tags for the final UI display
            clean_response = raw_response
            if "<response>" in clean_response:
                r_match = re.search(r'<response>(.*?)</response>', clean_response, re.DOTALL)
                if r_match:
                    clean_response = r_match.group(1).strip()
            
            # Remove thinking tags from clean_response regardless
            clean_response = re.sub(r'<thinking>.*?</thinking>', '', clean_response, flags=re.DOTALL).strip()
            clean_response = clean_response.replace("<response>", "").replace("</response>", "").strip()

            audit_log.raw_response = clean_response
            audit_log.raw_thinking = thinking
            
            clean_json_str = self._extract_json(raw_response)
            try:
                parsed_json = json.loads(clean_json_str)
                
                # Only inject deal extraction structure if it looks like a deal extraction skill
                # or if the keys are missing and it's explicitly requested.
                is_extraction = audit_log.skill and audit_log.skill.name == "deal_extraction"
                
                if is_extraction:
                    if "deal_model_data" not in parsed_json: parsed_json["deal_model_data"] = {}
                    if "metadata" not in parsed_json: parsed_json["metadata"] = {"ambiguous_points": [], "missing_fields": []}
                    if "analyst_report" not in parsed_json: parsed_json["analyst_report"] = raw_response
                
                # Include thinking in the parsed response for the UI
                parsed_json["thinking"] = thinking
                parsed_json["response"] = clean_response
                
                audit_log.parsed_json = parsed_json
                audit_log.is_success = True
                audit_log.status = 'COMPLETED'
                parsed_json["_raw_response"] = raw_response
            except:
                audit_log.is_success = False
                audit_log.status = 'FAILED'
                parsed_json = {"error": "JSON parsing error", "raw": raw_response, "thinking": thinking}
        except Exception as e:
            audit_log.is_success = False
            audit_log.status = 'FAILED'
            audit_log.error_message = str(e)
            parsed_json = {"error": str(e)}
        finally:
            audit_log.request_duration_ms = int((time.time() - start_time) * 1000)
            audit_log.save()
        return parsed_json
