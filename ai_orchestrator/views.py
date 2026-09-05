import logging
import json
import os
import uuid
from typing import Dict, Any, Optional, List
from django.db.models import Q, Count
from django.db import transaction
from django.forms.models import model_to_dict
from django.utils import timezone
from django.core.cache import cache
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.decorators import action
from rest_framework import status, viewsets
from django.http import StreamingHttpResponse

from .models import (
    AIPersonality, AISkill, AIConversation, AIMessage, AIAuditLog,
    DealIndustrySkillAssignment, DocumentChunk, AIPipelineDefinition,
    AIPromptRevision, AISkillRevision, VMControlOperation,
)
from .serializers import (
    AIConversationSerializer, AIMessageSerializer, AIAuditLogSerializer,
    AISkillSerializer,
)
from .services.ai_processor import AIProcessorService
from .services.chat_scope import ChatScopeValidationError, internal_citation, parse_chat_scope
from .services.embedding_processor import EmbeddingService
from .services.flow_config import UniversalChatFlowService
from .services.realtime import broadcast_audit_log_update
from .services.runtime import AIRuntimeService
from .services.industry_skills import IndustrySkillService
from .services.prompt_catalog import PromptCatalogService
from .services.pipeline_registry import PipelineRegistryService, RegistryValidationError
from .services.universal_chat import UniversalChatService
from .services.document_processor import DocumentProcessorService
from .services.chat_documents import ChatDocumentEvidenceService
from .services.vm_service import VMControlService
from deals.models import Deal, DealDocument, DealAnalysis, AnalysisKind, DealGeneratedDocument, DealRelationshipContext
from meetings.models import MeetingNote

logger = logging.getLogger(__name__)


def _is_ai_admin(user):
    return bool(
        user
        and user.is_authenticated
        and (
            user.is_superuser
            or user.is_staff
            or getattr(getattr(user, "profile", None), "is_admin", False)
        )
    )


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
        """
        log = self.get_object()
        task_id = log.celery_task_id
        source_meta = log.source_metadata or {}
        revoke_errors = []
        task_ids_to_revoke = [
            tid for tid in [
                task_id,
                source_meta.get("callback_task_id"),
                *(source_meta.get("child_task_ids") or []),
            ] if tid
        ]
        
        # 1. Kill the Celery worker thread immediately
        if task_ids_to_revoke:
            try:
                from config.celery import celery_app
                for revoke_id in dict.fromkeys(task_ids_to_revoke):
                    try:
                        celery_app.control.revoke(revoke_id, terminate=True, signal='SIGKILL')
                    except Exception as e:
                        revoke_errors.append(f"Failed to revoke task {revoke_id}: {e}")
                        logger.warning("Failed to revoke task %s for audit log %s: %s", revoke_id, log.id, e)
            except Exception as e:
                revoke_errors.append(f"Failed to connect to Celery broker: {e}")
                logger.warning("Failed to connect to Celery broker while cancelling audit log %s: %s", log.id, e)
            
        # 2. Update the log status; workers will stop cooperatively at task boundaries.
        log.source_metadata = {
            **source_meta,
            "cancel_requested": True,
            "cancel_requested_at": timezone.now().isoformat(),
            "cancel_reason": "manual",
            "cancelled_task_ids": task_ids_to_revoke,
        }
        log.status = 'FAILED'
        log.error_message = "Task manually terminated by forensic user."
        log.is_success = False
        log.save(update_fields=['source_metadata', 'status', 'error_message', 'is_success'])
        try:
            broadcast_audit_log_update(log, event_type="terminal", done=True)
        except Exception as e:
            revoke_errors.append(f"Failed to broadcast cancel update: {e}")
            logger.warning("Failed to broadcast cancel update for audit log %s: %s", log.id, e)

        if log.source_type == "vdr_indexing" and log.source_id:
            try:
                deal = Deal.objects.get(id=log.source_id)
                deal.processing_status = 'failed'
                deal.processing_error = "Task manually terminated by forensic user."
                deal.save(update_fields=['processing_status', 'processing_error'])
            except Deal.DoesNotExist:
                logger.warning("VDR cancel requested for missing deal %s", log.source_id)
            except Exception as e:
                revoke_errors.append(f"Failed to update deal processing state: {e}")
                logger.warning("Failed to update deal processing state for cancelled audit log %s: %s", log.id, e)
        
        response_payload = {
            "status": "cancelled",
            "task_id": task_id,
            "revoked_task_count": len(dict.fromkeys(task_ids_to_revoke)),
        }
        if revoke_errors:
            response_payload["warnings"] = revoke_errors
        return Response(response_payload)

class AIConversationViewSet(viewsets.ModelViewSet):
    serializer_class = AIConversationSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = AIConversation.objects.filter(user=self.request.user)
        kind = self.request.query_params.get("kind")
        if kind == "universal_chat":
            return qs.exclude(metadata__kind="deal_chat")
        elif kind == "deal_chat":
            deal_id = self.request.query_params.get("deal_id")
            qs = qs.filter(metadata__kind="deal_chat")
            if deal_id:
                qs = qs.filter(metadata__deal_id=str(deal_id))
            return qs
        return qs

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

class VMControlView(APIView):
    permission_classes = [IsAuthenticated]

    @staticmethod
    def _reconcile_operation(power_state):
        operation = VMControlOperation.objects.filter(
            status=VMControlOperation.Status.SUBMITTED,
        ).first()
        if not operation:
            return
        reached_target = (
            operation.action == VMControlOperation.Action.START and power_state == "running"
        ) or (
            operation.action == VMControlOperation.Action.DEALLOCATE and power_state == "deallocated"
        )
        if reached_target:
            operation.status = VMControlOperation.Status.SUCCEEDED
            operation.completed_at = timezone.now()
            operation.save(update_fields=["status", "completed_at"])

    def _snapshot(self, vm_service):
        snapshot = vm_service.snapshot()
        active_jobs = AIAuditLog.objects.filter(status__in=["PENDING", "PROCESSING"]).count()
        self._reconcile_operation(snapshot.power_state)
        latest = VMControlOperation.objects.first()
        allowed_actions = []
        if snapshot.control_enabled:
            if snapshot.power_state in {"deallocated", "stopped"}:
                allowed_actions.append("start")
            elif snapshot.power_state == "running" and active_jobs == 0:
                allowed_actions.append("deallocate")
        return {
            "control_enabled": snapshot.control_enabled,
            "target_label": snapshot.target_label,
            "power_state": snapshot.power_state,
            "service_state": snapshot.service_state,
            "startup_phase": snapshot.startup_phase,
            "services": snapshot.services,
            "active_ai_jobs": active_jobs,
            "allowed_actions": allowed_actions,
            "error": snapshot.error,
            "last_operation": {
                "id": str(latest.id),
                "action": latest.action,
                "status": latest.status,
                "requested_at": latest.requested_at.isoformat(),
                "completed_at": latest.completed_at.isoformat() if latest.completed_at else None,
                "failure_reason": latest.failure_reason,
            } if latest else None,
        }

    def get(self, request):
        if not _is_ai_admin(request.user):
            return Response({"detail": "Administrator access is required."}, status=status.HTTP_403_FORBIDDEN)
        return Response(self._snapshot(VMControlService()))

    def post(self, request):
        if not _is_ai_admin(request.user):
            return Response({"detail": "Administrator access is required."}, status=status.HTTP_403_FORBIDDEN)
        action = request.data.get("action")
        if action not in {"start", "deallocate"}:
            return Response({"detail": "action must be start or deallocate."}, status=status.HTTP_400_BAD_REQUEST)
        vm_service = VMControlService()
        if not vm_service.available:
            return Response({"detail": "VM control is unavailable."}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        if action == "deallocate" and AIAuditLog.objects.filter(status__in=["PENDING", "PROCESSING"]).exists():
            return Response({"detail": "Deallocation is blocked while AI work is active."}, status=status.HTTP_409_CONFLICT)
        lock_key = f"vm-control:{vm_service.subscription_id}:{vm_service.resource_group}:{vm_service.vm_name}"
        if not cache.add(lock_key, str(uuid.uuid4()), timeout=30):
            return Response({"detail": "Another VM command is being submitted."}, status=status.HTTP_409_CONFLICT)
        try:
            snapshot = vm_service.snapshot()
            if action == "start" and snapshot.power_state in {"running", "starting"}:
                return Response(self._snapshot(vm_service), status=status.HTTP_202_ACCEPTED)
            if action == "deallocate" and snapshot.power_state in {"deallocated", "deallocating"}:
                return Response(self._snapshot(vm_service), status=status.HTTP_202_ACCEPTED)
            if snapshot.power_state in {"starting", "deallocating"}:
                return Response({"detail": "The VM is already changing power state."}, status=status.HTTP_409_CONFLICT)
            operation = VMControlOperation.objects.create(
                action=action,
                target_label=vm_service.target_label,
                target_vm_name=vm_service.vm_name,
                target_resource_id=vm_service.target_resource_id,
                requested_by=request.user,
            )
            if action == "start":
                vm_service.start_vm()
            else:
                vm_service.stop_vm()
        except RuntimeError as exc:
            operation.status = VMControlOperation.Status.FAILED
            operation.failure_reason = str(exc)[:255]
            operation.completed_at = timezone.now()
            operation.save(update_fields=["status", "failure_reason", "completed_at"])
            return Response({"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        finally:
            cache.delete(lock_key)
        return Response(self._snapshot(vm_service), status=status.HTTP_202_ACCEPTED)

class DealChatView(APIView):
    """
    View to chat with the AI about a specific deal.
    """
    permission_classes = [IsAuthenticated]
    def post(self, request, deal_id=None):
        deal_id = deal_id or request.query_params.get('deal_id')
        user_message = request.data.get('message')
        stream = request.data.get('stream', True)
        if not deal_id or not user_message:
            return Response({"error": "deal_id and message are required"}, status=400)
        if not isinstance(user_message, str) or len(user_message.strip()) > 12000:
            return Response({"error": "message must be text no longer than 12,000 characters"}, status=400)
        user_message = user_message.strip()
        try:
            deal = Deal.objects.get(id=deal_id)
            try:
                scope = parse_chat_scope(request.data)
            except ChatScopeValidationError as exc:
                return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

            documents = list(
                deal.documents.filter(id__in=scope.document_ids, is_indexed=True)
            )
            if len(documents) != len(scope.document_ids):
                return Response(
                    {"error": "One or more selected documents are unavailable, unauthorized, or not indexed."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            transcripts = list(
                MeetingNote.objects.filter(
                    deals=deal,
                    id__in=scope.transcript_ids,
                    is_indexed=True,
                ).distinct()
            )
            if len(transcripts) != len(scope.transcript_ids):
                return Response(
                    {"error": "One or more selected transcripts are unavailable, unauthorized, or not indexed."},
                    status=status.HTTP_403_FORBIDDEN,
                )

            interactive_context_data = ""
            selected_sources: List[Dict[str, Any]] = []
            scope_diagnostics: Dict[str, Any] = {}
            if scope.has_private_scope:
                chat_service = UniversalChatService(AIProcessorService())
                query_plan = {
                    "user_query": user_message,
                    "selection_mode": "explicit_chat_scope",
                    "deal_ids": [str(deal.id)],
                }
                if scope.document_ids:
                    document_chunks, document_diagnostics = chat_service.chunks_for_selected_documents(
                        plan=query_plan,
                        deal_id=str(deal.id),
                        document_ids=scope.document_ids,
                    )
                    selected_sources.extend(document_chunks)
                    scope_diagnostics["documents"] = document_diagnostics
                if scope.transcript_ids:
                    transcript_chunks, transcript_diagnostics = chat_service.chunks_for_selected_transcripts(
                        deal_id=str(deal.id),
                        transcript_ids=scope.transcript_ids,
                    )
                    selected_sources.extend(transcript_chunks)
                    scope_diagnostics["transcripts"] = transcript_diagnostics
                if not selected_sources:
                    return Response(
                        {"error": "The selected scope has no retrievable indexed passages."},
                        status=status.HTTP_409_CONFLICT,
                    )
                interactive_context_data = chat_service.build_context_from_selection(
                    plan=query_plan,
                    deal_ids=[str(deal.id)],
                    chunks=selected_sources,
                    current_deal_id=str(deal.id),
                )

            personality = AIPersonality.objects.filter(is_default=True).first()
            skill = AISkill.objects.filter(name='deal_chat').first()
            citation_preview = [internal_citation(chunk) for chunk in selected_sources]

            conversation_id = request.data.get('conversation_id')
            if conversation_id:
                conversation = AIConversation.objects.filter(id=conversation_id, user=request.user).first()
                if not conversation:
                    return Response({"error": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND)
                conversation_metadata = conversation.metadata if isinstance(conversation.metadata, dict) else {}
                if (
                    conversation_metadata.get("kind") != "deal_chat"
                    or str(conversation_metadata.get("deal_id") or "") != str(deal.id)
                ):
                    return Response(
                        {"error": "This conversation belongs to a different chat context."},
                        status=status.HTTP_409_CONFLICT,
                    )
            else:
                conversation = AIConversation.objects.create(
                    user=request.user,
                    title=f"Chat: {deal.title}",
                    metadata={
                        "kind": "deal_chat",
                        "deal_id": str(deal.id),
                        "deal_title": deal.title,
                    },
                )
            
            # Create PENDING audit log for background tracking
            audit_log = AIRuntimeService.create_audit_log(
                source_type='deal_chat',
                source_id=str(deal.id),
                context_label=f"Deal Chat: {deal.title}",
                personality=personality,
                skill=skill,
                status='PENDING',
                is_success=False,
                system_prompt="Processing forensic query in background...",
                user_prompt=user_message,
                source_metadata={
                    "deal_id": str(deal.id),
                    "conversation_id": str(conversation.id),
                    "evidence_mode": scope.evidence_mode,
                    "web_search_enabled": scope.web_search_enabled,
                    "selected_document_ids": scope.document_ids,
                    "selected_transcript_ids": scope.transcript_ids,
                    "selected_sources": citation_preview,
                    "scope_diagnostics": scope_diagnostics,
                },
            )

            from .tasks import generate_chat_response_async
            
            # Save the user message to DB immediately (fixes Bug 1)
            user_chat_message = AIMessage.objects.create(
                conversation=conversation,
                role='user',
                content=user_message,
                applied_filters={
                    "audit_log_id": str(audit_log.id),
                    "evidence_mode": scope.evidence_mode,
                    "web_search_enabled": scope.web_search_enabled,
                    "selected_document_ids": scope.document_ids,
                    "selected_transcript_ids": scope.transcript_ids,
                },
            )

            model_provider = scope.model_provider
            if not isinstance(conversation.metadata, dict):
                conversation.metadata = {}
            conversation.metadata['model_provider'] = model_provider
            conversation.metadata['kind'] = 'deal_chat'
            conversation.metadata['deal_id'] = str(deal.id)
            conversation.metadata['deal_title'] = deal.title
            conversation.save(update_fields=['metadata'])

            task_info: Dict[str, Any] = {}

            def _enqueue_task():
                task = generate_chat_response_async.apply_async(
                    kwargs={
                        'conversation_id': str(conversation.id),
                        'user_message': user_message,
                        'skill_name': 'deal_chat',
                        'metadata': {
                            'deal_id': str(deal.id),
                            'model_provider': model_provider,
                            'web_search_enabled': scope.web_search_enabled,
                            'evidence_mode': scope.evidence_mode,
                            'selected_document_ids': scope.document_ids,
                            'selected_transcript_ids': scope.transcript_ids,
                            'interactive_context_data': interactive_context_data,
                            'selected_sources': selected_sources,
                            'user_message_id': str(user_chat_message.id),
                        },
                        'audit_log_id': str(audit_log.id)
                    }
                )
                task_info["id"] = task.id
                audit_log.celery_task_id = task.id
                audit_log.save(update_fields=['celery_task_id'])

            transaction.on_commit(_enqueue_task)

            return Response({
                "status": "queued",
                "task_id": task_info.get("id"),
                "audit_log_id": str(audit_log.id),
                "conversation_id": str(conversation.id),
                "evidence_mode": scope.evidence_mode,
                "selected_document_ids": scope.document_ids,
                "selected_transcript_ids": scope.transcript_ids,
            })
        except Deal.DoesNotExist:
            return Response({"error": "Deal not found."}, status=status.HTTP_404_NOT_FOUND)
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
        if not isinstance(user_message, str) or len(user_message.strip()) > 12000:
            return Response({"error": "message must be text no longer than 12,000 characters"}, status=400)
        user_message = user_message.strip()

        try:
            model_provider = str(request.data.get('model_provider') or 'vllm').strip().lower()
            if model_provider not in {'vllm', 'anthropic'}:
                return Response({"error": "model_provider must be either vllm or anthropic."}, status=400)
            web_search_enabled = request.data.get('web_search_enabled', False)
            if not isinstance(web_search_enabled, bool):
                return Response({"error": "web_search_enabled must be a boolean."}, status=400)
            if conversation_id:
                conversation = AIConversation.objects.filter(id=conversation_id, user=request.user).first()
                if not conversation:
                    return Response({"error": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND)
                conversation_metadata = conversation.metadata if isinstance(conversation.metadata, dict) else {}
                if conversation_metadata.get("kind") == "deal_chat":
                    deal_id = conversation_metadata.get("deal_id")
                    if deal_id:
                        view = DealChatView()
                        view.setup(request)
                        return view.post(request, deal_id=deal_id)
            else:
                conversation = AIConversation.objects.create(user=request.user, title=user_message[:50])

            chat_documents = (
                conversation.metadata.get('chat_documents', [])
                if isinstance(conversation.metadata, dict)
                else []
            )
            if model_provider == 'anthropic' and chat_documents:
                return Response(
                    {"error": "Private uploaded documents cannot be sent to the external Anthropic provider."},
                    status=400,
                )
            
            user_chat_message = AIMessage.objects.create(conversation=conversation, role='user', content=user_message)
            
            if not isinstance(conversation.metadata, dict):
                conversation.metadata = {}
            conversation.metadata['model_provider'] = model_provider
            conversation.metadata['kind'] = 'universal_chat'
            conversation.save(update_fields=['metadata'])

            personality = AIPersonality.objects.filter(is_default=True).first()
            skill = AISkill.objects.filter(name='universal_chat').first()
            
            # Create PENDING audit log for background tracking
            audit_log = AIRuntimeService.create_audit_log(
                source_type='universal_chat',
                source_id=str(conversation.id),
                context_label=f"Global Chat: {conversation.title}",
                personality=personality,
                skill=skill,
                status='PENDING',
                is_success=False,
                system_prompt="Queued for global pipeline query...",
                user_prompt=user_message,
            )

            from .tasks import generate_chat_response_async
            task_info: Dict[str, Any] = {}

            def _enqueue_task():
                task = generate_chat_response_async.apply_async(
                    kwargs={
                        'conversation_id': str(conversation.id),
                        'user_message': user_message,
                        'skill_name': 'universal_chat',
                        'metadata': {
                            'model_provider': model_provider,
                            'web_search_enabled': web_search_enabled,
                            'user_message_id': str(user_chat_message.id),
                        }, # We will build the context entirely inside the Celery task
                        'audit_log_id': str(audit_log.id)
                    }
                )
                task_info["id"] = task.id
                audit_log.celery_task_id = task.id
                audit_log.save(update_fields=['celery_task_id'])

            transaction.on_commit(_enqueue_task)

            return Response({
                "status": "queued",
                "task_id": task_info.get("id"),
                "audit_log_id": str(audit_log.id),
                "conversation_id": str(conversation.id)
            })
        except Exception as e:
            logger.error(f"Universal Chat error: {str(e)}", exc_info=True)
            return Response({"error": str(e)}, status=500)


class UniversalChatDocumentView(APIView):
    permission_classes = [IsAuthenticated]
    allowed_extensions = {".pdf", ".docx", ".xlsx", ".xls", ".pptx", ".txt", ".csv", ".png", ".jpg", ".jpeg"}
    max_file_size = 25 * 1024 * 1024
    max_documents = 5
    max_text_chars = 120_000

    def post(self, request):
        uploaded_file = request.FILES.get("file")
        conversation_id = request.data.get("conversation_id")
        if not uploaded_file:
            return Response({"error": "file is required"}, status=400)

        filename = os.path.basename(uploaded_file.name or "document")
        extension = os.path.splitext(filename)[1].lower()
        if extension not in self.allowed_extensions:
            return Response({"error": f"Unsupported file type: {extension or 'unknown'}"}, status=400)
        if uploaded_file.size > self.max_file_size:
            return Response({"error": "Document must be 25 MB or smaller."}, status=400)
        if conversation_id and not AIConversation.objects.filter(id=conversation_id, user=request.user).exists():
            return Response({"error": "Conversation not found."}, status=404)

        document_id = str(uuid.uuid4())
        result = DocumentProcessorService().get_chat_extraction_result(uploaded_file.read(), filename)
        extracted_text = str(result.get("normalized_text") or result.get("text") or "").strip()
        if not extracted_text:
            return Response({"error": result.get("error") or "No readable text was found in the document."}, status=422)

        with transaction.atomic():
            if conversation_id:
                conversation = AIConversation.objects.select_for_update().filter(
                    id=conversation_id,
                    user=request.user,
                ).first()
                if not conversation:
                    return Response({"error": "Conversation not found."}, status=404)
            else:
                conversation = AIConversation.objects.create(
                    user=request.user,
                    title=f"Chat with {filename}"[:255],
                )

            metadata = conversation.metadata if isinstance(conversation.metadata, dict) else {}
            documents = metadata.get("chat_documents") if isinstance(metadata.get("chat_documents"), list) else []
            if len(documents) >= self.max_documents:
                return Response({"error": f"A chat can contain up to {self.max_documents} documents."}, status=400)

            enriched = ChatDocumentEvidenceService.build(
                file_name=filename,
                extracted_text=extracted_text,
                extraction_mode=result.get("mode"),
                source_id=document_id,
                quality_flags=result.get("quality_flags") or [],
            )
            document = {
                "id": document_id,
                "name": filename,
                "size": uploaded_file.size,
                "extraction_mode": result.get("mode"),
                "quality_flags": enriched["evidence"].get("quality_flags") or [],
                "artifact_status": enriched["artifact_status"],
                "evidence": enriched["evidence"],
                "text": enriched["text"],
                "truncated": enriched["truncated"],
                "uploaded_at": timezone.now().isoformat(),
            }
            metadata["chat_documents"] = [*documents, document]
            conversation.metadata = metadata
            conversation.save(update_fields=["metadata", "updated_at"])

        return Response({
            "conversation_id": str(conversation.id),
            "document": ChatDocumentEvidenceService.public_metadata(document),
        }, status=201)

    def delete(self, request, document_id):
        conversation_id = request.data.get("conversation_id") or request.query_params.get("conversation_id")
        conversation = AIConversation.objects.filter(id=conversation_id, user=request.user).first()
        if not conversation:
            return Response({"error": "Conversation not found."}, status=404)

        metadata = conversation.metadata if isinstance(conversation.metadata, dict) else {}
        documents = metadata.get("chat_documents") if isinstance(metadata.get("chat_documents"), list) else []
        remaining = [document for document in documents if str(document.get("id")) != str(document_id)]
        if len(remaining) == len(documents):
            return Response({"error": "Document not found."}, status=404)
        metadata["chat_documents"] = remaining
        conversation.metadata = metadata
        conversation.save(update_fields=["metadata", "updated_at"])
        return Response(status=204)


class DealHelperView(APIView):
    permission_classes = [IsAuthenticated]
    cache_prefix = "deal_helper_session"
    session_ttl = 60 * 60 * 4

    def _cache_key(self, session_id: str) -> str:
        return f"{self.cache_prefix}:{session_id}"

    def _get_session(self, session_id: str) -> Dict[str, Any] | None:
        return cache.get(self._cache_key(session_id))

    def _save_session(self, session_id: str, payload: Dict[str, Any]) -> None:
        cache.set(self._cache_key(session_id), payload, timeout=self.session_ttl)

    def _profile(self, request):
        return getattr(request.user, "profile", None)

    def _touch_conversation(self, conversation: AIConversation) -> None:
        conversation.updated_at = timezone.now()
        conversation.save(update_fields=["updated_at"])

    def _helper_event(
        self,
        *,
        conversation: AIConversation,
        session_id: str,
        route: str | None,
        event_type: str,
        title: str,
        summary: str,
        data: Dict[str, Any] | None = None,
    ) -> AIMessage:
        message = AIMessage.objects.create(
            conversation=conversation,
            role="assistant",
            content=summary,
            data_points=data or {},
            applied_filters={
                "kind": "deal_helper_event",
                "event_type": event_type,
                "session_id": session_id,
                "route": route,
                "title": title,
            },
        )
        self._touch_conversation(conversation)
        return message

    def _summarize_chunks(self, chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [
            {
                "chunk_id": str(chunk.get("chunk_id") or ""),
                "deal_id": str(chunk.get("deal_id") or ""),
                "deal": chunk.get("deal") or "",
                "source_type": chunk.get("source_type") or "",
                "source_id": str(chunk.get("source_id") or ""),
                "source_title": chunk.get("source_title") or chunk.get("source_type") or "",
                "score": chunk.get("score"),
                "excerpt": str(chunk.get("text") or "")[:500],
                "is_current_deal": bool(chunk.get("is_current_deal")),
            }
            for chunk in chunks
        ]

    def _commit_relationship_context(
        self,
        *,
        request,
        deal: Deal,
        session: Dict[str, Any],
        selected_chunk_ids: List[str],
    ) -> None:
        if session.get("route") != "related_deals" or not session.get("selected_deal_ids"):
            return
        DealRelationshipContext.objects.create(
            deal=deal,
            related_deal=None,
            relationship_type=session.get("relationship_type") or DealRelationshipContext.RelationshipType.COMPARABLE,
            notes=session.get("relationship_notes") or "",
            selected_deal_ids=session.get("selected_deal_ids") or [],
            selected_document_ids=session.get("selected_document_ids") or [],
            selected_chunk_ids=selected_chunk_ids,
            created_by=self._profile(request),
        )

    def post(self, request, action: str):
        handlers = {
            "start": self.start,
            "select-deals": self.select_deals,
            "select-documents": self.select_documents,
            "answer": self.answer,
            "analysis": self.analysis,
        }
        handler = handlers.get(action)
        if not handler:
            return Response({"error": "Unsupported deal helper action."}, status=404)
        try:
            return handler(request)
        except Deal.DoesNotExist:
            return Response({"error": "Deal not found."}, status=404)
        except Exception as e:
            logger.error("Deal helper %s failed: %s", action, e, exc_info=True)
            return Response({"error": str(e)}, status=500)

    def _conversation_for_deal(self, request, deal: Deal, conversation_id: str | None = None, user_message: str | None = None):
        if conversation_id:
            conversation = AIConversation.objects.filter(id=conversation_id, user=request.user).first()
            if conversation:
                return conversation
        title_seed = (user_message or "").strip()
        if title_seed:
            title = f"{deal.title}: {title_seed[:64]}"
        else:
            title = f"{deal.title}: New Chat"
        return AIConversation.objects.create(
            user=request.user,
            title=title[:255],
            metadata={
                "kind": "deal_chat",
                "deal_id": str(deal.id),
                "deal_title": deal.title,
            },
        )

    def start(self, request):
        deal_id = request.data.get("deal_id")
        message = str(request.data.get("message") or "").strip()
        if not deal_id or not message:
            return Response({"error": "deal_id and message are required."}, status=400)
        deal = Deal.objects.get(id=deal_id)
        conversation = self._conversation_for_deal(request, deal, request.data.get("conversation_id"), message)
        
        # Save/update model_provider to conversation metadata
        model_provider = request.data.get('model_provider', 'vllm')
        if not isinstance(conversation.metadata, dict):
            conversation.metadata = {}
        conversation.metadata['model_provider'] = model_provider
        conversation.save(update_fields=['metadata'])
        from .tasks import _build_history_context
        user_message = AIMessage.objects.create(conversation=conversation, role="user", content=message)
        self._touch_conversation(conversation)
        history_context, _history_messages_used, _history_chars_used = _build_history_context(conversation)
        service = UniversalChatService(AIProcessorService())
        helper = service.start_deal_helper_session(
            deal_id=str(deal.id),
            user_message=message,
            conversation_id=str(conversation.id),
            history_context=history_context,
        )
        session_id = str(uuid.uuid4())
        payload = {
            "session_id": session_id,
            "deal_id": str(deal.id),
            "message": message,
            "conversation_id": str(conversation.id),
            "route": helper["route"],
            "query_plan": helper["query_plan"],
            "selected_deal_ids": [],
            "selected_document_ids": [],
            "selected_chunks": [],
            "relationship_type": None,
            "relationship_notes": "",
            "saved_context": helper.get("saved_context") or "",
            "user_message_id": str(user_message.id),
        }
        self._save_session(session_id, payload)
        self._helper_event(
            conversation=conversation,
            session_id=session_id,
            route=helper["route"],
            event_type="start",
            title="Started Deal Helper",
            summary=f"Started deal helper workflow: {helper['route'].replace('_', ' ')}.",
            data={
                "deal": {"id": str(deal.id), "title": deal.title},
                "message": message,
                "route": helper["route"],
                "candidate_deal_count": len(helper.get("candidate_deals") or []),
                "document_count": len(helper.get("documents") or []),
            },
        )
        return Response({
            **helper,
            "session_id": session_id,
            "conversation_id": str(conversation.id),
            "user_message_id": str(user_message.id),
            "deal": {"id": str(deal.id), "title": deal.title},
        })

    def select_deals(self, request):
        session_id = request.data.get("session_id")
        logger.info("[deal-helper] select_deals start session=%s", session_id)
        session = self._get_session(session_id)
        if not session:
            return Response({"error": "Session expired or not found."}, status=404)
        selected_deal_ids = [str(item) for item in request.data.get("selected_deal_ids", []) if item]
        if not selected_deal_ids:
            return Response({"error": "Select at least one deal."}, status=400)
        relationship_type = request.data.get("relationship_type") or DealRelationshipContext.RelationshipType.COMPARABLE
        notes = str(request.data.get("notes") or "").strip()
        # Offload to Celery
        from .tasks import discover_documents_async
        
        personality = AIRuntimeService.get_default_personality()
        skill = AIRuntimeService.get_skill("deal_chat")

        audit_log = AIRuntimeService.create_audit_log(
            source_type="deal_helper_discovery",
            source_id=session["deal_id"],
            context_label=f"Document Discovery for Session {session_id}",
            personality=personality,
            skill=skill,
            status="PENDING",
            is_success=False,
            user_prompt=session["message"],
        )

        task = discover_documents_async.apply_async(kwargs={
            "session_id": session_id,
            "query_plan": session["query_plan"],
            "deal_ids": selected_deal_ids,
            "current_deal_id": session["deal_id"],
            "audit_log_id": str(audit_log.id)
        })

        audit_log.celery_task_id = task.id
        audit_log.save(update_fields=["celery_task_id"])

        # Update session
        session.update({
            "selected_deal_ids": selected_deal_ids,
            "relationship_type": relationship_type,
            "relationship_notes": notes,
        })
        self._save_session(session_id, session)

        conversation = AIConversation.objects.filter(id=session["conversation_id"], user=request.user).first()
        if conversation:
            self._helper_event(
                conversation=conversation,
                session_id=session_id,
                route=session.get("route"),
                event_type="select_deals",
                title="Selected Related Deals",
                summary=f"Selected {len(selected_deal_ids)} related deal(s) as {relationship_type.replace('_', ' ')}. Document discovery queued.",
                data={
                    "relationship_type": relationship_type,
                    "notes": notes,
                    "audit_log_id": str(audit_log.id),
                    "task_id": task.id,
                },
            )
        return Response({
            "status": "queued",
            "task_id": task.id,
            "audit_log_id": str(audit_log.id),
            "documents": [],
            "chunks": [],
            "retrieval_diagnostics": {}
        })

    def select_documents(self, request):
        session_id = request.data.get("session_id")
        logger.info("[deal-helper] select_documents start session=%s", session_id)
        session = self._get_session(session_id)
        if not session:
            return Response({"error": "Session expired or not found."}, status=404)
        deal = Deal.objects.get(id=session["deal_id"])
        is_related_deal_flow = session.get("route") == "related_deals" and session.get("selected_deal_ids")
        if request.data.get("select_all_indexed"):
            if is_related_deal_flow:
                allowed_deal_ids = [str(deal.id), *[str(item) for item in session.get("selected_deal_ids") or []]]
                document_ids = [str(doc.id) for doc in DealDocument.objects.filter(deal_id__in=allowed_deal_ids, is_indexed=True)]
            else:
                document_ids = [str(doc.id) for doc in deal.documents.filter(is_indexed=True)]
        else:
            submitted_ids = [str(item) for item in request.data.get("document_ids", []) if item]
            vi_source_ids = [
                item for item in submitted_ids
                if item.startswith("vi_")
                and DocumentChunk.objects.filter(deal_id=deal.id, source_type="extracted_source", source_id=item).exists()
            ]
            document_ids = [item for item in submitted_ids if not item.startswith("vi_")]
            if is_related_deal_flow:
                allowed_deal_ids = [str(deal.id), *[str(item) for item in session.get("selected_deal_ids") or []]]
                indexed_ids = set(str(item) for item in DealDocument.objects.filter(deal_id__in=allowed_deal_ids, id__in=document_ids, is_indexed=True).values_list("id", flat=True))
                vi_source_ids = [
                    item for item in submitted_ids
                    if item.startswith("vi_")
                    and DocumentChunk.objects.filter(deal_id__in=allowed_deal_ids, source_type="extracted_source", source_id=item).exists()
                ]
            else:
                indexed_ids = set(str(item) for item in deal.documents.filter(id__in=document_ids, is_indexed=True).values_list("id", flat=True))
            document_ids = [item for item in document_ids if item in indexed_ids] + vi_source_ids
        if not document_ids:
            return Response({"error": "Select at least one indexed document."}, status=400)
        
        # Offload to Celery
        from .tasks import discover_chunks_async
        
        personality = AIRuntimeService.get_default_personality()
        skill = AIRuntimeService.get_skill("deal_chat")

        audit_log = AIRuntimeService.create_audit_log(
            source_type="deal_helper_discovery",
            source_id=session["deal_id"],
            context_label=f"Chunk Discovery for Session {session_id}",
            personality=personality,
            skill=skill,
            status="PENDING",
            is_success=False,
            user_prompt=session["message"],
        )

        task = discover_chunks_async.apply_async(kwargs={
            "session_id": session_id,
            "query_plan": session["query_plan"],
            "document_ids": document_ids,
            "current_deal_id": str(deal.id),
            "is_multi_deal": is_related_deal_flow,
            "audit_log_id": str(audit_log.id)
        })

        audit_log.celery_task_id = task.id
        audit_log.save(update_fields=["celery_task_id"])

        # Update session
        session.update({
            "selected_deal_ids": session.get("selected_deal_ids") if is_related_deal_flow else [str(deal.id)],
            "selected_document_ids": document_ids,
        })
        self._save_session(session_id, session)

        conversation = AIConversation.objects.filter(id=session["conversation_id"], user=request.user).first()
        if conversation:
            self._helper_event(
                conversation=conversation,
                session_id=session_id,
                route=session.get("route"),
                event_type="select_documents",
                title="Selected Documents",
                summary=f"Selected {len(document_ids)} document(s). Chunk discovery queued.",
                data={
                    "selected_document_ids": document_ids,
                    "audit_log_id": str(audit_log.id),
                    "task_id": task.id,
                },
            )
        return Response({
            "status": "queued",
            "task_id": task.id,
            "audit_log_id": str(audit_log.id),
            "chunks": [],
            "retrieval_diagnostics": {}
        })

    def answer(self, request):
        session_id = request.data.get("session_id")
        session = self._get_session(session_id)
        if not session:
            return Response({"error": "Session expired or not found."}, status=404)
        selected_chunk_ids = {str(item) for item in request.data.get("selected_chunk_ids", []) if item}
        candidate_chunks = session.get("candidate_chunks") or []
        chunks = [chunk for chunk in candidate_chunks if str(chunk.get("chunk_id")) in selected_chunk_ids]
        if not chunks:
            return Response({"error": "Select at least one chunk."}, status=400)
        deal = Deal.objects.get(id=session["deal_id"])
        conversation = self._conversation_for_deal(request, deal, session.get("conversation_id"), session.get("message"))
        self._commit_relationship_context(
            request=request,
            deal=deal,
            session=session,
            selected_chunk_ids=list(selected_chunk_ids),
        )
        self._helper_event(
            conversation=conversation,
            session_id=session_id,
            route=session.get("route"),
            event_type="select_chunks",
            title="Selected Evidence Chunks",
            summary=f"Selected {len(chunks)} evidence chunk(s) for answer generation.",
            data={"chunks": self._summarize_chunks(chunks)},
        )
        service = UniversalChatService(AIProcessorService())
        selected_deal_ids = session.get("selected_deal_ids") or [str(deal.id)]
        extra_context = "\n".join(
            item for item in [
                session.get("saved_context") or "",
                f"Relationship type: {session.get('relationship_type')}" if session.get("relationship_type") else "",
                f"Analyst notes: {session.get('relationship_notes')}" if session.get("relationship_notes") else "",
                str(request.data.get("notes") or "").strip(),
            ] if item
        )
        context_data = service.build_context_from_selection(
            plan=session["query_plan"],
            deal_ids=selected_deal_ids,
            chunks=chunks,
            extra_context=extra_context,
            current_deal_id=session["deal_id"]
        )
        personality = AIPersonality.objects.filter(is_default=True).first()
        skill = AISkill.objects.filter(name='deal_chat').first()
        audit_log = AIRuntimeService.create_audit_log(
            source_type='deal_chat',
            source_id=str(deal.id),
            context_label=f"Deal Helper: {deal.title}",
            personality=personality,
            skill=skill,
            status='PENDING',
            is_success=False,
            system_prompt="Queued interactive deal helper answer...",
            user_prompt=session["message"],
            source_metadata={
                "deal_helper_session_id": session_id,
                "route": session.get("route"),
                "selected_deal_ids": selected_deal_ids,
                "selected_document_ids": session.get("selected_document_ids") or [],
                "selected_chunk_ids": list(selected_chunk_ids),
            },
        )
        from .tasks import generate_chat_response_async
        task = generate_chat_response_async.apply_async(kwargs={
            "conversation_id": str(conversation.id),
            "user_message": session["message"],
            "skill_name": "deal_chat",
            "metadata": {
                "deal_id": str(deal.id),
                "model_provider": (conversation.metadata or {}).get("model_provider", "vllm"),
                "interactive_context_data": context_data,
                "query_plan": session["query_plan"],
                "selected_sources": [
                    f"{chunk.get('deal')}|{chunk.get('source_title') or chunk.get('source_type')}"
                    for chunk in chunks
                ],
            },
            "audit_log_id": str(audit_log.id),
        })
        audit_log.celery_task_id = task.id
        audit_log.save(update_fields=["celery_task_id"])
        self._helper_event(
            conversation=conversation,
            session_id=session_id,
            route=session.get("route"),
            event_type="answer_queued",
            title="Queued Answer",
            summary="Queued answer generation from the selected evidence.",
            data={
                "audit_log_id": str(audit_log.id),
                "task_id": task.id,
                "selected_chunk_ids": list(selected_chunk_ids),
            },
        )
        session["selected_chunks"] = chunks
        self._save_session(session_id, session)
        return Response({
            "status": "queued",
            "task_id": task.id,
            "audit_log_id": str(audit_log.id),
            "conversation_id": str(conversation.id),
        })

    def analysis(self, request):
        deal_id = request.data.get("deal_id")
        directive = str(request.data.get("directive") or "").strip()
        mode = request.data.get("mode") or "user_directive_addendum"
        document_title = str(request.data.get("document_title") or "").strip()
        session_id = request.data.get("session_id")
        selected_chunk_ids = [str(item) for item in request.data.get("selected_chunk_ids", []) if item]
        model_provider = request.data.get("model_provider", "vllm")
        if not deal_id or not directive:
            return Response({"error": "deal_id and directive are required."}, status=400)
        if mode == "full_rewrite":
            return Response({"error": "Full rewrite is no longer supported from deal helper."}, status=400)
        deal = Deal.objects.get(id=deal_id)
        selected_context = ""
        selected_deal_ids = []
        selected_document_ids = []
        helper_session = {}
        candidate_chunks = []
        if session_id:
            helper_session = self._get_session(session_id) or {}
            selected_deal_ids = helper_session.get("selected_deal_ids") or []
            selected_document_ids = helper_session.get("selected_document_ids") or []
            candidate_chunks = helper_session.get("candidate_chunks") or []
            selected_context = "\n\n".join(
                f"[{chunk.get('deal')} | {chunk.get('source_title') or chunk.get('source_type')}]\n{chunk.get('text') or ''}"
                for chunk in candidate_chunks
                if not selected_chunk_ids or str(chunk.get("chunk_id")) in selected_chunk_ids
            )
        personality = AIPersonality.objects.filter(is_default=True).first()
        skill = AISkill.objects.filter(name='deal_helper_directive_document').first() or AISkill.objects.filter(name='deal_chat').first()
        generated_document = DealGeneratedDocument.objects.create(
            deal=deal,
            title=document_title or directive[:80] or "Directive Document",
            kind=DealGeneratedDocument.DocumentKind.DIRECTIVE,
            directive=directive,
            content="Queued...",
            selected_deal_ids=selected_deal_ids,
            selected_document_ids=selected_document_ids,
            selected_chunk_ids=selected_chunk_ids,
            created_by=self._profile(request),
        )
        audit_log = AIRuntimeService.create_audit_log(
            source_type='deal_helper_analysis',
            source_id=str(deal.id),
            context_label=f"Deal Helper Analysis: {deal.title}",
            personality=personality,
            skill=skill,
            status='PENDING',
            is_success=False,
            system_prompt="Queued user-directed deal analysis...",
            user_prompt=directive,
            source_metadata={
                "mode": mode,
                "generated_document_id": str(generated_document.id) if generated_document else None,
                "selected_chunk_ids": selected_chunk_ids,
                "model_provider": model_provider,
            },
        )
        audit_log.model_provider = model_provider
        audit_log.save(update_fields=["model_provider"])
        from .tasks import generate_deal_helper_analysis_async
        task = generate_deal_helper_analysis_async.apply_async(kwargs={
            "deal_id": str(deal.id),
            "directive": directive,
            "mode": mode,
            "audit_log_id": str(audit_log.id),
            "document_title": document_title,
            "generated_document_id": str(generated_document.id) if generated_document else None,
            "selected_context": selected_context,
            "selected_deal_ids": selected_deal_ids,
            "selected_document_ids": selected_document_ids,
            "selected_chunk_ids": selected_chunk_ids,
            "model_provider": model_provider,
        }, queue="high_priority")
        if generated_document:
            generated_document.audit_log_id = str(audit_log.id)
            generated_document.save(update_fields=["audit_log_id"])
        audit_log.celery_task_id = task.id
        audit_log.save(update_fields=["celery_task_id"])
        if helper_session:
            self._commit_relationship_context(
                request=request,
                deal=deal,
                session=helper_session,
                selected_chunk_ids=selected_chunk_ids,
            )
            conversation = AIConversation.objects.filter(id=helper_session.get("conversation_id"), user=request.user).first()
            if conversation:
                selected_chunks = [
                    chunk for chunk in candidate_chunks
                    if not selected_chunk_ids or str(chunk.get("chunk_id")) in selected_chunk_ids
                ]
                if selected_chunks:
                    self._helper_event(
                        conversation=conversation,
                        session_id=session_id,
                        route=helper_session.get("route"),
                        event_type="select_chunks",
                        title="Selected Evidence Chunks",
                        summary=f"Selected {len(selected_chunks)} evidence chunk(s) for saved analysis.",
                        data={"chunks": self._summarize_chunks(selected_chunks)},
                    )
                self._helper_event(
                    conversation=conversation,
                    session_id=session_id,
                    route=helper_session.get("route"),
                    event_type="analysis_queued",
                    title="Queued Saved Analysis",
                    summary="Queued directive document from the selected evidence.",
                    data={
                        "mode": mode,
                        "directive": directive,
                        "document_title": document_title,
                        "audit_log_id": str(audit_log.id),
                        "task_id": task.id,
                        "generated_document_id": str(generated_document.id) if generated_document else None,
                        "selected_chunk_ids": selected_chunk_ids,
                    },
                )
        return Response({
            "status": "queued",
            "task_id": task.id,
            "audit_log_id": str(audit_log.id),
            "message": "Analysis queued.",
        })

import threading

# Global fallback state for local AI connection status
LOCAL_AI_STATE = {
    "vm_online": False,
    "vm_status": "unknown",
    "available_models": [],
    "telemetry": {"loaded_models": []},
    "status": "pending",
    "checked_at": 0
}

def _run_connection_probe():
    global LOCAL_AI_STATE
    from django.core.cache import cache
    import time
    from .services.ai_processor import AIProcessorService
    from .services.vm_service import VMControlService
    
    logger.info("Executing thread-based background local AI connection probe...")

    ai_service = AIProcessorService()
    vm_service = VMControlService()

    vm_online = False
    available_models = []
    vm_status = "unknown"

    try:
        vm_status = vm_service.get_status()
    except Exception as e:
        logger.warning("Failed to check VM status in thread: %s", e)

    try:
        vm_online = ai_service.provider.health_check()
        if vm_online:
            available_models = ai_service.provider.get_available_models()
    except Exception as e:
        logger.warning("vLLM connectivity probe failed in thread: %s", e)

    result = {
        "vm_online": vm_online,
        "vm_status": vm_status,
        "available_models": available_models,
        "telemetry": {
            "loaded_models": [{"name": m, "vram_gb": "unknown"} for m in available_models]
        },
        "status": "completed",
        "checked_at": time.time()
    }

    LOCAL_AI_STATE = result
    try:
        cache.set("local_ai_connection_status", result, timeout=300)
    except Exception as e:
        logger.warning("Failed to set connection cache in thread: %s", e)


def trigger_background_check(force=False):
    global LOCAL_AI_STATE
    import time
    from django.core.cache import cache
    
    now = time.time()
    
    # Try fetching from cache first
    cached_status = None
    try:
        cached_status = cache.get("local_ai_connection_status")
    except Exception as e:
        logger.warning("Cache access failed (Redis might be down): %s", e)
        
    if cached_status:
        LOCAL_AI_STATE = cached_status
        
    # Check if we need to trigger a check
    should_trigger = False
    if force:
        should_trigger = True
    elif LOCAL_AI_STATE.get("status") != "pending" and now - LOCAL_AI_STATE.get("checked_at", 0) > 60:
        should_trigger = True
        
    if should_trigger:
        LOCAL_AI_STATE["status"] = "pending"
        LOCAL_AI_STATE["vm_status"] = "checking..."
        LOCAL_AI_STATE["checked_at"] = now
        
        try:
            cache.set("local_ai_connection_status", LOCAL_AI_STATE, timeout=30)
        except Exception:
            pass
            
        # 1. Try to trigger via Celery
        try:
            from .tasks import check_local_ai_connection_task
            check_local_ai_connection_task.delay()
            logger.info("Triggered connection check Celery task successfully.")
        except Exception as e:
            # 2. Fallback to background thread
            logger.warning("Celery dispatch failed (Redis down/refused). Falling back to background thread: %s", e)
            thread = threading.Thread(target=_run_connection_probe)
            thread.daemon = True
            thread.start()
            
    return LOCAL_AI_STATE


class AIConnectionStatusView(APIView):
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        force_refresh = request.GET.get("refresh") == "true"
        status_data = trigger_background_check(force=force_refresh)
        return Response(status_data)


class ForexRateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .services.forex_service import ForexService

        quote = ForexService().get_quote()
        return Response({
            **quote.as_dict(),
            "canonical_currency": "INR",
            "supported_units": {
                "crore": 10_000_000,
                "million": 1_000_000,
            },
            "supported_display_currencies": ["INR", "USD"],
        })


def _pipeline_inventory() -> list[dict]:
    """Return registered topology plus the latest observed execution per stage."""
    pipelines = AIPipelineDefinition.objects.filter(is_active=True).prefetch_related(
        "stages__prompt_definition__revisions", "stages__skill__revisions"
    ).order_by("name")
    active_statuses = ("PENDING", "PROCESSING")
    active_counts = {
        row["pipeline_stage_id"]: row["count"]
        for row in AIAuditLog.objects.filter(
            pipeline_stage_id__isnull=False,
            status__in=active_statuses,
        ).values("pipeline_stage_id").annotate(count=Count("id"))
    }
    latest_by_stage = {}
    for log in AIAuditLog.objects.filter(
        pipeline_stage_id__isnull=False,
    ).only(
        "id", "pipeline_stage_id", "status", "created_at", "completed_at",
        "request_duration_ms", "context_label", "source_type", "error_message",
    ).order_by("-created_at")[:2000]:
        latest_by_stage.setdefault(log.pipeline_stage_id, log)

    result = []
    for pipeline in pipelines:
        stages = []
        for stage in pipeline.stages.all().order_by("position", "name"):
            latest = latest_by_stage.get(stage.id)
            stage_active_runs = active_counts.get(stage.id, 0)
            runtime = {
                "status": "PROCESSING" if stage_active_runs else (latest.status if latest else "IDLE"),
                "active_runs": stage_active_runs,
                "last_run": {
                    "id": str(latest.id),
                    "status": latest.status,
                    "started_at": latest.created_at,
                    "completed_at": latest.completed_at,
                    "duration_ms": latest.request_duration_ms,
                    "label": latest.context_label,
                    "source_type": latest.source_type,
                    "error": latest.error_message,
                } if latest else None,
            }
            shared = {
                "id": str(stage.id), "key": stage.key, "name": stage.name,
                "position": stage.position, "depends_on": stage.depends_on,
                "is_required": stage.is_required, "runtime": runtime,
            }
            if stage.prompt_definition_id:
                revisions = list(stage.prompt_definition.revisions.order_by("-revision"))
                active = next((row for row in revisions if row.status == AIPromptRevision.Status.PUBLISHED), None)
                stages.append({
                    **shared,
                    "kind": "prompt", "required_variables": stage.required_variables,
                    "definition_key": stage.prompt_definition.key,
                    "description": stage.prompt_definition.description,
                    "is_guardrail": stage.prompt_definition.is_guardrail,
                    "active_revision": _serialize_prompt_revision(active),
                    "revisions": [_serialize_prompt_revision(row) for row in revisions[:20]],
                })
            elif stage.skill_id:
                revisions = list(stage.skill.revisions.order_by("-revision"))
                active = next((row for row in revisions if row.status == AISkillRevision.Status.PUBLISHED), None)
                stages.append({
                    **shared,
                    "kind": "skill", "required_variables": stage.required_variables,
                    "skill_id": str(stage.skill_id), "skill_name": stage.skill.name,
                    "description": stage.skill.description,
                    "active_revision": _serialize_skill_revision(active),
                    "revisions": [_serialize_skill_revision(row) for row in revisions[:20]],
                })
            else:
                stages.append({
                    **shared,
                    "kind": "operation",
                    "description": stage.description,
                    "required_variables": [],
                    "active_revision": None,
                    "revisions": [],
                })
        pipeline_active_runs = sum(stage["runtime"]["active_runs"] for stage in stages)
        result.append({
            "key": pipeline.key,
            "name": pipeline.name,
            "description": pipeline.description,
            "kind": "catalog" if pipeline.key.startswith("legacy_") else "runtime",
            "active_runs": pipeline_active_runs,
            "stages": stages,
        })
    return result


def _serialize_prompt_revision(revision):
    if not revision:
        return None
    return {
        "id": str(revision.id), "revision": revision.revision, "status": revision.status,
        "system_template": revision.system_template, "user_template": revision.user_template,
        "input_schema": revision.input_schema, "output_schema": revision.output_schema,
        "created_at": revision.created_at, "published_at": revision.published_at,
    }


def _serialize_skill_revision(revision):
    if not revision:
        return None
    return {
        "id": str(revision.id), "revision": revision.revision, "status": revision.status,
        "system_template": revision.system_template, "prompt_template": revision.prompt_template,
        "input_schema": revision.input_schema, "output_schema": revision.output_schema,
        "created_at": revision.created_at, "published_at": revision.published_at,
    }


class AISettingsView(APIView):
    permission_classes = [IsAuthenticated]
    def get(self, request):
        if not _is_ai_admin(request.user):
            return Response(
                {"error": "AI settings are only available to administrators."},
                status=status.HTTP_403_FORBIDDEN,
            )
        try:
            from django.conf import settings
            from .models import AnalysisProtocol, AISystemSetting
            from .serializers import AIPersonalitySerializer, AISkillSerializer, AnalysisProtocolSerializer
            
            personalities = AIPersonality.objects.all()
            skills = AISkill.objects.all()
            protocols = AnalysisProtocol.objects.all()
            flow_state = UniversalChatFlowService.serialize_state()
            PipelineRegistryService.sync_legacy_defaults()
            
            status_data = trigger_background_check(force=False)

            # Live Forex
            from .services.forex_service import ForexService
            forex = ForexService()
            live_rate = forex.get_crore_string()

            # Claude settings override
            claude_setting = AISystemSetting.objects.filter(key="CLAUDE_TEXT_MODEL").first()
            claude_text_model = claude_setting.value if claude_setting else getattr(settings, "CLAUDE_TEXT_MODEL", "claude-haiku-4-5-20251001")

            return Response({
                "personalities": AIPersonalitySerializer(personalities, many=True).data,
                "skills": AISkillSerializer(skills, many=True).data,
                "protocols": AnalysisProtocolSerializer(protocols, many=True).data,
                "universal_chat_flow": flow_state,
                "available_models": status_data.get("available_models", []),
                "telemetry": status_data.get("telemetry", {"loaded_models": []}),
                "vm_online": status_data.get("vm_online", False),
                "vm_status": status_data.get("vm_status", "unknown"),
                "live_rate": live_rate,
                "claude_text_model": claude_text_model,
                "web_search": {
                    "provider": "SearXNG",
                    "base_url": getattr(settings, "SEARXNG_BASE_URL", "http://localhost:8081"),
                    "language": getattr(settings, "SEARXNG_LANGUAGE", "en-IN"),
                    "engines": list(getattr(settings, "SEARXNG_ENGINES", [])),
                },
                "prompt_catalog": PromptCatalogService.serialize(),
                "pipelines": _pipeline_inventory(),
            })
        except Exception as e: 
            return Response({"error": str(e)}, status=500)



    def post(self, request):
        """
        Update settings for personalities, skills, or protocols.
        """
        if not _is_ai_admin(request.user):
            return Response(
                {"error": "AI settings can only be changed by an administrator."},
                status=status.HTTP_403_FORBIDDEN,
            )
        try:
            from .models import AnalysisProtocol
            
            target_type = request.data.get("type") # 'personality', 'skill', 'protocol', 'flow'
            target_id = request.data.get("id")
            updates = request.data.get("updates", {})
            action = updates.get('action')
            
            import random, string
            def rand_suffix(): return "".join(random.choices(string.ascii_lowercase + string.digits, k=4))

            if target_type == 'system_setting':
                key = target_id
                value = updates.get('value')
                if key and value is not None:
                    from .models import AISystemSetting
                    setting, _ = AISystemSetting.objects.get_or_create(key=key)
                    setting.value = value
                    setting.save()
                    return Response({"success": True})
                return Response({"error": "Key and value are required"}, status=400)

            if target_type == 'prompt':
                try:
                    if updates.get('action') == 'reset':
                        PromptCatalogService.reset(str(target_id))
                    else:
                        PromptCatalogService.update(str(target_id), updates.get('value'))
                except ValueError as exc:
                    return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
                return Response({"success": True})

            if target_type == 'pipeline_prompt':
                try:
                    stage = PipelineRegistryService.resolve_stage(*str(target_id).split('.', 1)).stage
                    if stage.kind != stage.Kind.PROMPT:
                        raise RegistryValidationError("This pipeline stage is backed by a skill.")
                    if action == 'publish':
                        revision = AIPromptRevision.objects.get(
                            id=updates.get('revision_id'), definition=stage.prompt_definition,
                        )
                        PipelineRegistryService.publish_prompt(revision, published_by=request.user)
                    elif action == 'rollback':
                        source = AIPromptRevision.objects.get(
                            id=updates.get('revision_id'), definition=stage.prompt_definition,
                        )
                        revision = PipelineRegistryService.create_prompt_draft(
                            stage.prompt_definition,
                            user_template=source.user_template,
                            system_template=source.system_template,
                            input_schema=source.input_schema,
                            output_schema=source.output_schema,
                            created_by=request.user,
                        )
                        PipelineRegistryService.publish_prompt(revision, published_by=request.user)
                    else:
                        active = PipelineRegistryService.resolve_stage(
                            stage.pipeline.key, stage.key
                        ).prompt_revision
                        if updates.get('user_template') is not None:
                            proposed_template = str(updates.get('user_template'))
                            system_template = str(updates.get('system_template', active.system_template))
                        else:
                            proposed_template = PipelineRegistryService.compose_business_edit(
                                active.user_template,
                                str(updates.get('business_template') or ''),
                            )
                            system_template = active.system_template
                        revision = PipelineRegistryService.create_prompt_draft(
                            stage.prompt_definition,
                            user_template=proposed_template,
                            system_template=system_template,
                            input_schema=active.input_schema,
                            output_schema=active.output_schema,
                            created_by=request.user,
                        )
                    return Response({"success": True, "revision": _serialize_prompt_revision(revision)})
                except (ValueError, AIPromptRevision.DoesNotExist, RegistryValidationError) as exc:
                    return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

            if target_type == 'pipeline_skill':
                try:
                    stage = PipelineRegistryService.resolve_stage(*str(target_id).split('.', 1)).stage
                    if stage.kind != stage.Kind.SKILL:
                        raise RegistryValidationError("This pipeline stage is backed by a prompt.")
                    if action == 'publish':
                        revision = AISkillRevision.objects.get(id=updates.get('revision_id'), skill=stage.skill)
                        PipelineRegistryService.publish_skill(revision, published_by=request.user)
                    elif action == 'rollback':
                        source = AISkillRevision.objects.get(id=updates.get('revision_id'), skill=stage.skill)
                        revision = PipelineRegistryService.create_skill_draft(
                            stage.skill, system_template=source.system_template,
                            prompt_template=source.prompt_template, input_schema=source.input_schema,
                            output_schema=source.output_schema, created_by=request.user,
                        )
                        PipelineRegistryService.publish_skill(revision, published_by=request.user)
                    else:
                        active = PipelineRegistryService.resolve_stage(
                            stage.pipeline.key, stage.key
                        ).skill_revision
                        if updates.get('prompt_template') is not None:
                            proposed_template = str(updates.get('prompt_template'))
                            system_template = str(updates.get('system_template', active.system_template))
                        else:
                            proposed_template = PipelineRegistryService.compose_business_edit(
                                active.prompt_template,
                                str(updates.get('business_template') or ''),
                            )
                            system_template = active.system_template
                        revision = PipelineRegistryService.create_skill_draft(
                            stage.skill,
                            system_template=system_template,
                            prompt_template=proposed_template,
                            input_schema=active.input_schema, output_schema=active.output_schema,
                            created_by=request.user,
                        )
                    return Response({"success": True, "revision": _serialize_skill_revision(revision)})
                except (ValueError, AISkillRevision.DoesNotExist, RegistryValidationError) as exc:
                    return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

            if target_type == 'simulate_prompt':
                template = str(updates.get('template', ''))
                variables = updates.get('variables', {}) or {}
                required_vars = updates.get('required_variables', []) or []
                try:
                    rendered = PipelineRegistryService.render(template, variables, required_vars)
                    return Response({"success": True, "rendered_prompt": rendered})
                except Exception as exc:
                    return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

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
                    requested_name = str(updates.get('name', 'New Skill')).strip()
                    skill_name = requested_name
                    if AISkill.objects.filter(name=skill_name).exists():
                        skill_name = f"{requested_name} {rand_suffix()}"
                    serializer = AISkillSerializer(data={
                        "name": skill_name,
                        "description": updates.get('description', ''),
                        "system_template": updates.get('system_template', ''),
                        "prompt_template": updates.get('prompt_template', ''),
                        "input_schema": updates.get('input_schema', {}),
                        "output_schema": updates.get('output_schema', {}),
                        "skill_format": updates.get(
                            'skill_format',
                            AISkill.Format.NATIVE_PROMPT_V1,
                        ),
                        "is_industry_overview_eligible": bool(
                            updates.get('is_industry_overview_eligible', False)
                        ),
                        "status": AISkill.Status.DRAFT,
                    })
                    if not serializer.is_valid():
                        return Response(
                            serializer.errors,
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                    serializer.save(owner=request.user)
                elif action == 'approve':
                    obj = AISkill.objects.get(id=target_id)
                    if not obj.prompt_template.strip():
                        return Response(
                            {"error": "A non-empty prompt template is required for approval."},
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                    obj.status = AISkill.Status.APPROVED
                    obj.approved_by = request.user
                    obj.approved_at = timezone.now()
                    obj.save(update_fields=[
                        'status', 'approved_by', 'approved_at', 'updated_at',
                    ])
                elif action in {'retire', 'delete'}:
                    obj = AISkill.objects.get(id=target_id)
                    obj.status = AISkill.Status.RETIRED
                    obj.is_industry_overview_eligible = False
                    obj.save(update_fields=[
                        'status', 'is_industry_overview_eligible', 'updated_at',
                    ])
                else:
                    obj = AISkill.objects.get(id=target_id)
                    editable_fields = {
                        'name', 'description', 'system_template', 'prompt_template',
                        'input_schema', 'output_schema', 'skill_format',
                        'is_industry_overview_eligible',
                    }
                    unknown_fields = set(updates) - editable_fields
                    if unknown_fields:
                        return Response(
                            {
                                "error": "Unsupported skill fields: "
                                + ", ".join(sorted(unknown_fields))
                            },
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                    serializer = AISkillSerializer(
                        obj,
                        data=updates,
                        partial=True,
                    )
                    if not serializer.is_valid():
                        return Response(
                            serializer.errors,
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                    versioned_fields = {
                        'system_template', 'prompt_template', 'input_schema',
                        'output_schema', 'skill_format',
                    }
                    prompt_changed = any(
                        field in serializer.validated_data
                        and serializer.validated_data[field] != getattr(obj, field)
                        for field in versioned_fields
                    )
                    saved = serializer.save()
                    if prompt_changed:
                        saved.version += 1
                        saved.status = AISkill.Status.DRAFT
                        saved.approved_by = None
                        saved.approved_at = None
                        saved.save(update_fields=[
                            'version', 'status', 'approved_by', 'approved_at',
                            'updated_at',
                        ])
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
        skills = AISkill.objects.all().order_by('name')
        if not _is_ai_admin(request.user):
            skills = skills.filter(
                status=AISkill.Status.APPROVED,
                is_industry_overview_eligible=True,
            )
        elif request.query_params.get("industry_overview") == "true":
            skills = skills.filter(is_industry_overview_eligible=True)
        return Response({
            "skills": AISkillSerializer(skills, many=True).data,
            "compatibility": {
                "format": AISkill.Format.CLAUDE_PROMPT_V1,
                "kind": "prompt_only",
                "allowed": [
                    "name", "description", "system_template", "prompt_template",
                    "input_schema", "output_schema",
                ],
                "forbidden": [
                    "code", "scripts", "executables", "tool definitions",
                    "network actions", "file actions",
                ],
            },
        })

    def post(self, request):
        skill_id = request.data.get("skill_id")
        deal_id = request.data.get("deal_id")
        if not skill_id or not deal_id:
            return Response(
                {"error": "skill_id and deal_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            skill = AISkill.objects.filter(id=skill_id).first()
        except (DjangoValidationError, ValueError):
            return Response(
                {"error": "skill_id must be a valid UUID."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if (
            not skill
            or skill.status != AISkill.Status.APPROVED
            or not skill.is_industry_overview_eligible
        ):
            return Response(
                {"error": "The selected skill is not approved for Industry Overview."},
                status=status.HTTP_403_FORBIDDEN,
            )
        try:
            deal = Deal.objects.filter(id=deal_id).first()
        except (DjangoValidationError, ValueError):
            return Response(
                {"error": "deal_id must be a valid UUID."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not deal:
            return Response(
                {"error": "Deal not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            inputs = AIRuntimeService.validate_skill_inputs(
                skill,
                request.data.get("inputs", {}),
            )
        except ValueError as exc:
            return Response(
                {"error": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        document_ids = request.data.get("source_document_ids", [])
        if not isinstance(document_ids, list) or len(document_ids) > 20:
            return Response(
                {"error": "source_document_ids must be a list of at most 20 IDs."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            documents = list(
                DealDocument.objects.filter(
                    deal=deal,
                    id__in=document_ids,
                ).order_by('created_at')
            )
        except (DjangoValidationError, ValueError):
            return Response(
                {"error": "source_document_ids must contain valid UUIDs."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(documents) != len(set(str(value) for value in document_ids)):
            return Response(
                {"error": "Every source document must belong to the selected deal."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        source_metadata = {
            "run_kind": "industry_skill",
            "skill_version": skill.version,
            "input_scope": {
                "deal_id": str(deal.id),
                "input_keys": sorted(inputs),
                "source_document_ids": [str(document.id) for document in documents],
            },
            "sources": [
                {
                    "document_id": str(document.id),
                    "title": document.title,
                    "document_type": document.document_type,
                }
                for document in documents
            ],
        }
        audit_log = AIRuntimeService.create_audit_log(
            source_type="industry_skill",
            source_id=str(deal.id),
            context_label=f"Industry skill: {skill.name} — {deal.title}",
            skill=skill,
            status="PENDING",
            is_success=False,
            requested_by=request.user,
            skill_version=skill.version,
            source_metadata=source_metadata,
        )
        content = AIRuntimeService.build_industry_skill_context(
            deal,
            inputs,
            documents,
        )
        try:
            result = AIProcessorService().process_content(
                content=content,
                skill_name=skill.name,
                metadata={
                    **inputs,
                    "audit_log_id": str(audit_log.id),
                    "_source_metadata": source_metadata,
                    "context_label": audit_log.context_label,
                },
                source_id=str(deal.id),
                source_type="industry_skill",
            )
        except Exception as exc:
            audit_log.status = "FAILED"
            audit_log.is_success = False
            audit_log.error_message = str(exc)
            audit_log.completed_at = timezone.now()
            audit_log.save(update_fields=[
                'status', 'is_success', 'error_message', 'completed_at',
            ])
            return Response(
                {
                    "audit_log_id": str(audit_log.id),
                    "status": "FAILED",
                    "error": "Skill execution failed.",
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )

        audit_log.refresh_from_db()
        response_status = (
            status.HTTP_200_OK
            if audit_log.status == "COMPLETED"
            else status.HTTP_502_BAD_GATEWAY
        )
        return Response(
            {
                "audit_log_id": str(audit_log.id),
                "status": audit_log.status,
                "skill_id": str(skill.id),
                "skill_version": audit_log.skill_version,
                "output": audit_log.raw_response,
                "parsed_output": audit_log.parsed_json,
                "sources": source_metadata["sources"],
                "result": result,
            },
            status=response_status,
        )


def _can_manage_deal_industry_skills(user, deal):
    if user.is_superuser or user.is_staff:
        return True
    profile = getattr(user, "profile", None)
    return bool(
        profile
        and not profile.is_disabled
        and (profile.is_admin or deal.responsibility.filter(id=profile.id).exists())
    )


class DealIndustrySkillsView(APIView):
    permission_classes = [IsAuthenticated]

    @staticmethod
    def _deal(deal_id):
        return Deal.objects.filter(id=deal_id).first()

    def get(self, request, deal_id):
        deal = self._deal(deal_id)
        if not deal:
            return Response({"error": "Deal not found."}, status=404)
        assignments = list(
            DealIndustrySkillAssignment.objects.filter(deal=deal)
            .select_related("skill", "last_audit_log")
            .order_by("skill__name")
        )
        for assignment in assignments:
            IndustrySkillService.enqueue_automatic(assignment)
        assignments = list(
            DealIndustrySkillAssignment.objects.filter(deal=deal)
            .select_related("skill", "last_audit_log")
            .order_by("skill__name")
        )
        skills = AISkill.objects.filter(
            status=AISkill.Status.APPROVED,
            is_industry_overview_eligible=True,
        ).order_by("name")
        return Response({
            "can_manage": _can_manage_deal_industry_skills(request.user, deal),
            "eligible_skills": AISkillSerializer(skills, many=True).data,
            "assignments": [IndustrySkillService.serialize(item) for item in assignments],
        })


class DealIndustrySkillAssignmentView(APIView):
    permission_classes = [IsAuthenticated]

    def _resolve(self, deal_id, skill_id):
        deal = Deal.objects.filter(id=deal_id).first()
        skill = AISkill.objects.filter(id=skill_id).first()
        return deal, skill

    def put(self, request, deal_id, skill_id):
        deal, skill = self._resolve(deal_id, skill_id)
        if not deal:
            return Response({"error": "Deal not found."}, status=404)
        if not _can_manage_deal_industry_skills(request.user, deal):
            return Response({"error": "You do not have permission to manage this deal."}, status=403)
        if (
            not skill
            or skill.status != AISkill.Status.APPROVED
            or not skill.is_industry_overview_eligible
        ):
            return Response(
                {"error": "The selected skill is not approved for Industry Overview."},
                status=403,
            )
        inputs = request.data.get("inputs", {})
        try:
            inputs = AIRuntimeService.validate_skill_inputs(skill, inputs)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=400)
        document_ids = request.data.get("source_document_ids", [])
        if not isinstance(document_ids, list) or len(document_ids) > 20:
            return Response(
                {"error": "source_document_ids must be a list of at most 20 IDs."},
                status=400,
            )
        try:
            valid_count = DealDocument.objects.filter(
                deal=deal, id__in=document_ids
            ).count()
        except (DjangoValidationError, ValueError):
            return Response({"error": "source_document_ids must contain valid UUIDs."}, status=400)
        if valid_count != len(set(str(value) for value in document_ids)):
            return Response(
                {"error": "Every source document must belong to the selected deal."},
                status=400,
            )
        assignment, _created = DealIndustrySkillAssignment.objects.update_or_create(
            deal=deal,
            skill=skill,
            defaults={
                "enabled": bool(request.data.get("enabled", True)),
                "auto_run": bool(request.data.get("auto_run", False)),
                "inputs": inputs,
                "source_document_ids": [str(value) for value in document_ids],
                "configured_by": request.user,
            },
        )
        assignment = DealIndustrySkillAssignment.objects.select_related(
            "skill", "last_audit_log", "deal"
        ).get(id=assignment.id)
        queued = IndustrySkillService.enqueue_automatic(assignment)
        assignment.refresh_from_db()
        return Response({
            "queued": queued,
            "assignment": IndustrySkillService.serialize(assignment),
        })

    def delete(self, request, deal_id, skill_id):
        deal, _skill = self._resolve(deal_id, skill_id)
        if not deal:
            return Response({"error": "Deal not found."}, status=404)
        if not _can_manage_deal_industry_skills(request.user, deal):
            return Response({"error": "You do not have permission to manage this deal."}, status=403)
        assignment = DealIndustrySkillAssignment.objects.filter(
            deal=deal, skill_id=skill_id
        ).first()
        if not assignment:
            return Response({"error": "Assignment not found."}, status=404)
        assignment.enabled = False
        assignment.auto_run = False
        assignment.configured_by = request.user
        assignment.save(update_fields=["enabled", "auto_run", "configured_by", "updated_at"])
        return Response(status=204)


class DealIndustrySkillRunView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, deal_id, skill_id):
        deal = Deal.objects.filter(id=deal_id).first()
        if not deal:
            return Response({"error": "Deal not found."}, status=404)
        if not _can_manage_deal_industry_skills(request.user, deal):
            return Response({"error": "You do not have permission to run skills for this deal."}, status=403)
        assignment = DealIndustrySkillAssignment.objects.filter(
            deal=deal, skill_id=skill_id
        ).select_related("deal", "skill", "last_audit_log").first()
        if not assignment:
            return Response({"error": "Assignment not found."}, status=404)
        try:
            audit_log = IndustrySkillService.run(
                assignment.id,
                requested_by=request.user,
                trigger="manual",
            )
        except PermissionError as exc:
            return Response({"error": str(exc)}, status=403)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=400)
        assignment.refresh_from_db()
        response_status = 200 if audit_log.status == "COMPLETED" else 502
        return Response(
            {"assignment": IndustrySkillService.serialize(assignment)},
            status=response_status,
        )
