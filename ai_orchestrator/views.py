import logging
import json
import uuid
from typing import Dict, Any, Optional, List
from django.db.models import Q, Count
from django.db import transaction
from django.forms.models import model_to_dict
from django.utils import timezone
from django.core.cache import cache
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
from .services.realtime import broadcast_audit_log_update
from .services.runtime import AIRuntimeService
from .services.universal_chat import UniversalChatService
from .services.vm_service import VMControlService
from deals.models import Deal, DealDocument, DealAnalysis, AnalysisKind, DealGeneratedDocument, DealRelationshipContext

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
            ai_service = AIProcessorService()
            personality = AIPersonality.objects.filter(is_default=True).first()
            skill = AISkill.objects.filter(name='deal_chat').first()
            
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

            task_info: Dict[str, Any] = {}

            def _enqueue_task():
                task = generate_chat_response_async.apply_async(
                    kwargs={
                        'conversation_id': str(conversation.id),
                        'user_message': user_message,
                        'skill_name': 'deal_chat',
                        'metadata': {
                            'deal_id': str(deal.id),
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
                        'metadata': {}, # We will build the context entirely inside the Celery task
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
            document_ids = [str(item) for item in request.data.get("document_ids", []) if item]
            if is_related_deal_flow:
                allowed_deal_ids = [str(deal.id), *[str(item) for item in session.get("selected_deal_ids") or []]]
                indexed_ids = set(str(item) for item in DealDocument.objects.filter(deal_id__in=allowed_deal_ids, id__in=document_ids, is_indexed=True).values_list("id", flat=True))
            else:
                indexed_ids = set(str(item) for item in deal.documents.filter(id__in=document_ids, is_indexed=True).values_list("id", flat=True))
            document_ids = [item for item in document_ids if item in indexed_ids]
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
        if not deal_id or not directive:
            return Response({"error": "deal_id and directive are required."}, status=400)
        deal = Deal.objects.get(id=deal_id)
        if mode == "full_rewrite":
            deal.analysis_prompt = directive
            deal.save(update_fields=["analysis_prompt"])
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
        if mode == "full_rewrite":
            skill = AISkill.objects.filter(name='deal_synthesis').first() or AISkill.objects.filter(name='vdr_incremental_analysis').first() or AISkill.objects.filter(name='deal_chat').first()
        else:
            skill = AISkill.objects.filter(name='deal_helper_directive_document').first() or AISkill.objects.filter(name='deal_chat').first()
        generated_document = None
        if mode != "full_rewrite":
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
            },
        )
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
                    summary=f"Queued {'full rewrite' if mode == 'full_rewrite' else 'directive document'} from the selected evidence.",
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

class AISettingsView(APIView):
    permission_classes = [IsAuthenticated]
    def get(self, request):
        try:
            from .models import AnalysisProtocol
            from .serializers import AIPersonalitySerializer, AISkillSerializer, AnalysisProtocolSerializer
            
            ai_service = AIProcessorService()
            vm_service = VMControlService()
            
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
                vm_online = ai_service.provider.health_check()
                if vm_online:
                    available_models = ai_service.provider.get_available_models()
            except Exception as e:
                logger.warning("vLLM connectivity probe failed: %s", e)

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
            return Response([{"id": str(s.id), "name": s.name, "description": s.description, "prompt_template": s.prompt_template, "system_template": s.system_template} for s in skills])
        except Exception as e: return Response({"error": str(e)}, status=500)
