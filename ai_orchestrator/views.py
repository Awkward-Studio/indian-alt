import logging
import json
import time
import requests
import uuid
from typing import Dict, Any, Optional, List
from django.db.models import Q, Count
from django.forms.models import model_to_dict
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.decorators import action
from rest_framework import status, viewsets
from django.http import StreamingHttpResponse

from .models import AIPersonality, AISkill, AIConversation, AIMessage, AIAuditLog
from .serializers import AIConversationSerializer, AIMessageSerializer, AIAuditLogSerializer
from .services.ai_processor import AIProcessorService
from .services.embedding_processor import EmbeddingService
from .services.flow_config import UniversalChatFlowService
from .services.universal_chat import UniversalChatService
from .services.vm_service import VMControlService
from deals.models import Deal

logger = logging.getLogger(__name__)

class AIAuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for viewing AI Audit Logs.
    """
    queryset = AIAuditLog.objects.all().order_by('-created_at')
    serializer_class = AIAuditLogSerializer
    permission_classes = [IsAuthenticated]

    # Standard retrieve will now use the enhanced AIAuditLogSerializer
    # which includes system_prompt, raw fields, and parsed_json automatically.

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """
        Attempts to cancel a running task using its stored Celery ID.
        Also attempts to clear the Ollama VM state if possible.
        """
        log = self.get_object()
        task_id = log.celery_task_id
        
        # 1. Kill the Celery worker thread immediately
        if task_id:
            from config.celery import celery_app
            celery_app.control.revoke(task_id, terminate=True, signal='SIGKILL')
            
        # 2. Force-clear Ollama VM (send a tiny request to interrupt long generation)
        try:
            from .services.ai_processor import AIProcessorService
            ai = AIProcessorService()
            # Most LLM servers interrupt the previous request if a new one comes in with specific flags,
            # or we just ensure the connection is closed.
            # For Ollama, the best way to free VRAM/Process is to load a tiny model or 
            # send a request with num_predict: 1
            requests.post(f"{ai.ollama_url}/api/generate", json={
                "model": log.model_used,
                "prompt": "stop",
                "options": {"num_predict": 1},
                "stream": False
            }, timeout=5)
        except:
            pass

        # 3. Update the log status
        log.status = 'FAILED'
        log.error_message = "Task manually terminated by forensic user."
        log.save()
        
        return Response({"status": "cancelled", "task_id": task_id})

class AIConversationViewSet(viewsets.ModelViewSet):
    serializer_class = AIConversationSerializer
    permission_classes = [IsAuthenticated]
    def get_queryset(self):
        return AIConversation.objects.filter(user=self.request.user)
    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

class VMControlView(APIView):
    permission_classes = [IsAuthenticated]
    def get(self, request):
        vm_service = VMControlService()
        return Response({"status": vm_service.get_status()})
    def post(self, request):
        action = request.data.get('action')
        vm_service = VMControlService()
        success = vm_service.start_vm() if action == 'start' else vm_service.stop_vm()
        return Response({"success": success})

class DealChatView(APIView):
    """
    View to chat with the AI about a specific deal.
    """
    permission_classes = [IsAuthenticated]
    def post(self, request):
        deal_id = request.query_params.get('deal_id')
        user_message = request.data.get('message')
        stream = request.data.get('stream', True)
        if not deal_id or not user_message:
            return Response({"error": "deal_id and message are required"}, status=400)
        try:
            deal = Deal.objects.get(id=deal_id)
            
            # Fetch timeline/phase logs
            phase_logs = deal.phase_logs.all().order_by('changed_at')
            timeline = []
            for log in phase_logs:
                timeline.append({
                    "from": log.from_phase,
                    "to": log.to_phase,
                    "rationale": log.rationale,
                    "timestamp": log.changed_at.isoformat(),
                    "changed_by": log.changed_by.user.email if log.changed_by and log.changed_by.user else "System"
                })

            # Create a rich, structured representation of the deal's forensic data
            structured_data = {
                "title": deal.title,
                "industry": deal.industry,
                "sector": deal.sector,
                "funding_ask": deal.funding_ask,
                "priority": deal.priority,
                "current_phase": deal.current_phase,
                "themes": deal.themes if isinstance(deal.themes, list) else [],
                "ambiguities": deal.ambiguities if isinstance(deal.ambiguities, list) else [],
                "forensic_summary": deal.deal_summary,
                "status_flags": {
                    "female_led": deal.is_female_led,
                    "management_meeting": deal.management_meeting,
                    "proposal_stage": deal.business_proposal_stage,
                    "ic_stage": deal.ic_stage
                },
                "timeline_history": timeline
            }
            
            embed_service = EmbeddingService()
            chunks = embed_service.search_similar_chunks(user_message, deal, limit=8)
            
            rag_context = f"DEAL FORENSIC RECORD:\n{json.dumps(structured_data, default=str, indent=2)}\n\nRAW DOCUMENT CHUNKS (MOST RELEVANT TO QUERY):\n"
            if chunks:
                for chunk in chunks:
                    rag_context += f"\n--- {chunk.metadata.get('filename', 'Source')} ---\n{chunk.content}\n"
            else:
                rag_context += "No specific raw document chunks matched this query."
            
            ai_service = AIProcessorService()
            personality = AIPersonality.objects.filter(is_default=True).first()
            skill = AISkill.objects.filter(name='deal_chat').first()
            
            # Use model from personality
            default_model = personality.text_model_name if personality else 'qwen3.5:latest'

            # Create PENDING audit log for background tracking
            audit_log = AIAuditLog.objects.create(
                source_type='deal_chat',
                source_id=str(deal.id),
                context_label=f"Deal Chat: {deal.title}",
                personality=personality,
                skill=skill,
                status='PENDING',
                is_success=False,
                model_used=default_model,
                system_prompt="Processing forensic query in background...",
                user_prompt=user_message
            )

            from .tasks import generate_chat_response_async
            
            # We don't have a conversation object in DealChat currently, 
            # but we might need one if we want persistent history. 
            # For now, we'll try to find or create one for the deal.
            conversation, _ = AIConversation.objects.get_or_create(
                user=request.user,
                title=f"Chat: {deal.title}",
                defaults={'id': uuid.uuid4()} # Using deal ID as a reference? No, use new UUID.
            )

            task = generate_chat_response_async.apply_async(
                kwargs={
                    'conversation_id': str(conversation.id),
                    'user_message': user_message,
                    'skill_name': 'deal_chat',
                    'metadata': {'deal_context': rag_context},
                    'audit_log_id': str(audit_log.id)
                }
            )

            audit_log.celery_task_id = task.id
            audit_log.save(update_fields=['celery_task_id'])

            return Response({
                "status": "queued",
                "task_id": task.id,
                "audit_log_id": str(audit_log.id),
                "conversation_id": str(conversation.id)
            })
        except Exception as e:
            logger.error(f"Deal Chat error: {str(e)}", exc_info=True)
            return Response({"error": str(e)}, status=500)

class UniversalChatView(APIView):
    permission_classes = [IsAuthenticated]
    def post(self, request):
        user_message = request.data.get('message')
        history = request.data.get('history', [])
        conversation_id = request.data.get('conversation_id')
        stream = request.data.get('stream', True)
        if not user_message: return Response({"error": "message is required"}, status=400)

        try:
            if conversation_id:
                try: 
                    conversation = AIConversation.objects.get(id=conversation_id, user=request.user)
                except: 
                    conversation = AIConversation.objects.create(user=request.user, title=user_message[:50])
            else:
                conversation = AIConversation.objects.create(user=request.user, title=user_message[:50])
            
            AIMessage.objects.create(conversation=conversation, role='user', content=user_message)
            
            personality = AIPersonality.objects.filter(is_default=True).first()
            skill = AISkill.objects.filter(name='universal_chat').first()
            
            # Use model from personality
            default_model = personality.text_model_name if personality else 'qwen3.5:latest'

            # Create PENDING audit log for background tracking
            audit_log = AIAuditLog.objects.create(
                source_type='universal_chat',
                source_id=str(conversation.id),
                context_label=f"Global Chat: {conversation.title}",
                personality=personality,
                skill=skill,
                status='PENDING',
                is_success=False,
                model_used=default_model,
                system_prompt="Queued for global pipeline query...",
                user_prompt=user_message
            )

            from .tasks import generate_chat_response_async
            task = generate_chat_response_async.apply_async(
                kwargs={
                    'conversation_id': str(conversation.id),
                    'user_message': user_message,
                    'skill_name': 'universal_chat',
                    'metadata': {}, # We will build the context entirely inside the Celery task
                    'audit_log_id': str(audit_log.id)
                }
            )

            audit_log.celery_task_id = task.id
            audit_log.save(update_fields=['celery_task_id'])

            return Response({
                "status": "queued",
                "task_id": task.id,
                "audit_log_id": str(audit_log.id),
                "conversation_id": str(conversation.id)
            })
        except Exception as e:
            logger.error(f"Universal Chat error: {str(e)}", exc_info=True)
            return Response({"error": str(e)}, status=500)

class AISettingsView(APIView):
    permission_classes = [IsAuthenticated]
    def get(self, request):
        try:
            from .models import AnalysisProtocol
            from .serializers import AIPersonalitySerializer, AISkillSerializer, AnalysisProtocolSerializer
            
            ai_service = AIProcessorService()
            vm_service = VMControlService()
            ollama_url = ai_service.provider.ollama_url
            
            personalities = AIPersonality.objects.all()
            skills = AISkill.objects.all()
            protocols = AnalysisProtocol.objects.all()
            flow_state = UniversalChatFlowService.serialize_state()
            
            # Fast Check for VM Connectivity
            vm_online = False
            available_models = []
            telemetry = {"loaded_models": []}
            vm_status = vm_service.get_status()
            
            try:
                # Use the same Ollama endpoint the rest of the app is configured to use.
                # The previous 1.5s timeout was too aggressive for remote GPU hosts and
                # could mark the link offline while normal inference still worked.
                tags_resp = requests.get(f"{ollama_url}/api/tags", timeout=5)
                if tags_resp.status_code == 200:
                    vm_online = True
                    available_models = [m['name'] for m in tags_resp.json().get('models', [])]
                    
                    # If online, try fetching telemetry
                    ps_resp = requests.get(f"{ollama_url}/api/ps", timeout=5)
                    if ps_resp.status_code == 200:
                        for model in ps_resp.json().get('models', []):
                            vram_gb = model.get('size_vram', 0) / 1e9
                            telemetry["loaded_models"].append({
                                "name": model.get('name'), 
                                "vram_gb": round(vram_gb, 2)
                            })
            except Exception as e:
                logger.warning("Neural Engine connectivity probe failed for %s: %s", ollama_url, e)

            # If Azure reports the VM is running but the short telemetry probe missed,
            # expose the actual VM status separately while keeping vm_online tied to
            # successful Ollama connectivity.

            # Live Forex
            from .services.forex_service import ForexService
            forex = ForexService()
            live_rate = forex.get_crore_string()

            return Response({
                "personalities": AIPersonalitySerializer(personalities, many=True).data,
                "skills": AISkillSerializer(skills, many=True).data,
                "protocols": AnalysisProtocolSerializer(protocols, many=True).data,
                "universal_chat_flow": flow_state,
                "available_models": available_models,
                "telemetry": telemetry,
                "vm_online": vm_online,
                "vm_status": vm_status,
                "live_rate": live_rate
            })
        except Exception as e: 
            return Response({"error": str(e)}, status=500)

    def post(self, request):
        """
        Update settings for personalities, skills, or protocols.
        """
        try:
            from .models import AnalysisProtocol
            
            target_type = request.data.get("type") # 'personality', 'skill', 'protocol', 'flow'
            target_id = request.data.get("id")
            updates = request.data.get("updates", {})
            action = updates.get('action')
            
            import random, string
            def rand_suffix(): return "".join(random.choices(string.ascii_lowercase + string.digits, k=4))

            if target_type == 'personality':
                if target_id == 'new':
                    AIPersonality.objects.create(
                        name=f"{updates.get('name', 'New Personality')} {rand_suffix()}",
                        description=updates.get('description', ''),
                        system_instructions=updates.get('system_instructions', 'You are...'),
                        is_default=False
                    )
                elif action == 'delete':
                    obj = AIPersonality.objects.get(id=target_id)
                    if not obj.is_default:
                        obj.delete()
                else:
                    obj = AIPersonality.objects.get(id=target_id)
                    for k, v in updates.items(): setattr(obj, k, v)
                    obj.save()
            elif target_type == 'skill':
                if target_id == 'new':
                    AISkill.objects.create(
                        name=f"{updates.get('name', 'New Skill')} {rand_suffix()}",
                        description=updates.get('description', ''),
                        prompt_template=updates.get('prompt_template', '')
                    )
                elif action == 'delete':
                    AISkill.objects.get(id=target_id).delete()
                else:
                    obj = AISkill.objects.get(id=target_id)
                    for k, v in updates.items(): setattr(obj, k, v)
                    obj.save()
            elif target_type == 'protocol':
                if target_id == 'new':
                    AnalysisProtocol.objects.create(
                        name=f"{updates.get('name', 'New Protocol')} {rand_suffix()}",
                        directives=updates.get('directives', []),
                        is_active=False
                    )
                elif action == 'delete':
                    obj = AnalysisProtocol.objects.get(id=target_id)
                    if not obj.is_active:
                        obj.delete()
                else:
                    obj = AnalysisProtocol.objects.get(id=target_id)
                    for k, v in updates.items(): setattr(obj, k, v)
                    obj.save()
            elif target_type == 'flow':
                if target_id != 'universal_chat':
                    return Response({"error": "Unsupported flow target"}, status=400)

                if action == 'create_draft':
                    draft = UniversalChatFlowService.create_draft_from_published()
                    return Response({"success": True, "draft_version_id": str(draft.id)})

                if action == 'publish':
                    published = UniversalChatFlowService.publish_draft()
                    return Response({"success": True, "published_version_id": str(published.id)})

                if action == 'test':
                    query = str(updates.get("query") or "").strip()
                    if not query:
                        return Response({"error": "A test query is required."}, status=400)
                    flow_state = UniversalChatFlowService.serialize_state()
                    draft_version = flow_state.get("draft_version")
                    draft_config = draft_version.get("config") if draft_version else None
                    published_version = flow_state.get("published_version") or {}
                    chat_service = UniversalChatService(
                        AIProcessorService(),
                        flow_config=draft_config or published_version.get("config"),
                    )
                    return Response({
                        "success": True,
                        "simulation": chat_service.simulate_query(query)
                    })

                config = updates.get("config")
                if not isinstance(config, dict):
                    return Response({"error": "Flow updates require a config object."}, status=400)
                draft = UniversalChatFlowService.update_draft(config)
                return Response({"success": True, "draft_version_id": str(draft.id)})
                
            return Response({"success": True})
        except Exception as e: 
            logger.error(f"Error in AISettingsView.post: {str(e)}")
            return Response({"error": str(e)}, status=500)

class AISkillsView(APIView):
    permission_classes = [IsAuthenticated]
    def get(self, request):
        try:
            skills = AISkill.objects.all().order_by('name')
            return Response([{"id": str(s.id), "name": s.name, "description": s.description, "prompt_template": s.prompt_template} for s in skills])
        except Exception as e: return Response({"error": str(e)}, status=500)
