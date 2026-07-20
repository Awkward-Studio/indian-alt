import logging
import json
import time
import re
from celery import shared_task
from .models import AIAuditLog, AIMessage, AIConversation, AIPersonality, AISkill
from .services.ai_processor import AIProcessorService
from .services.universal_chat import UniversalChatService
from .services.runtime import AIRuntimeService

from .services.realtime import broadcast_ai_stream_delta, broadcast_audit_log_update

logger = logging.getLogger(__name__)

CHAT_HISTORY_MESSAGE_LIMIT = 3
CHAT_HISTORY_CHAR_LIMIT = 12000

DEAL_CHAT_CONVERSATIONAL_PROMPT = """[CHAT HISTORY]
{{ history_context }}

[AVAILABLE DEAL CONTEXT]
{{ context_data }}

[USER MESSAGE]
{{ content }}

[RESPONSE STYLE]
Answer conversationally as a deal chat assistant. Be direct, useful, and grounded in the available deal context.
Do not write a formal report, memo, diligence document, or long structured analysis unless the user explicitly asks for that artifact.
Use bullets or a small table only when it makes the answer easier to scan.
If the context does not contain enough evidence, say what is missing instead of inventing facts.

[VISUAL OUTPUT]
When the user asks for a graph, chart, visual, infographic, timeline, KPI view, or comparison, include exactly one fenced deal_visual JSON block when the available evidence supports it.
Do not invent values for a visual. If the data is incomplete, explain what is missing instead of emitting a visual.
The visual block must be valid JSON only, with no comments or trailing commas, in this shape:
```deal_visual
{
  "version": 1,
  "type": "bar",
  "title": "Short visual title",
  "summary": "One sentence explaining the takeaway.",
  "unit": "INR Cr",
  "data": [
    {"label": "FY22", "value": 120},
    {"label": "FY23", "value": 180}
  ],
  "source_notes": ["Source document or context note"]
}
```
Supported type values are: bar, line, area, pie, donut, kpi_strip, timeline, comparison_matrix.
For kpi_strip data rows use {"label": "...", "value": "...", "unit": "...", "tone": "positive|neutral|negative"}.
For timeline data rows use {"label": "...", "date": "...", "description": "...", "tone": "positive|neutral|negative"}.
For comparison_matrix data rows use {"label": "...", "values": {"Company A": "...", "Company B": "..."}}.
Wrap the visual with a short Markdown explanation before or after it.
"""


def _split_leaked_thinking(response: str, thinking: str = "") -> tuple[str, str]:
    response = response or ""
    thinking_parts = [thinking.strip()] if thinking and thinking.strip() else []

    def capture_thinking(match):
        body = (match.group(2) or "").strip()
        if body:
            thinking_parts.append(body)
        return ""

    cleaned_response = re.sub(
        r"<(thinking|think)>(.*?)</\1>",
        capture_thinking,
        response,
        flags=re.IGNORECASE | re.DOTALL,
    )

    lower_response = cleaned_response.lower()
    close_positions = [
        (lower_response.rfind("</think>"), "</think>"),
        (lower_response.rfind("</thinking>"), "</thinking>"),
    ]
    close_index, close_tag = max(close_positions, key=lambda item: item[0])
    if close_index >= 0:
        leaked_thinking = cleaned_response[:close_index].strip()
        if leaked_thinking:
            thinking_parts.append(leaked_thinking)
        cleaned_response = cleaned_response[close_index + len(close_tag):]

    response_match = re.search(r"<response>(.*?)(?:</response>|$)", cleaned_response, flags=re.IGNORECASE | re.DOTALL)
    if response_match:
        cleaned_response = response_match.group(1)

    cleaned_response = re.sub(r"</?(thinking|think|response)>", "", cleaned_response, flags=re.IGNORECASE).strip()
    cleaned_thinking = "\n\n".join(part for part in thinking_parts if part)
    return cleaned_response, cleaned_thinking


def _build_history_context(conversation: AIConversation) -> tuple[str, int, int]:
    previous_messages = list(
        AIMessage.objects.filter(conversation=conversation)
        .order_by('-created_at')[1 : CHAT_HISTORY_MESSAGE_LIMIT + 1]
    )

    entries = [f"{msg.role.upper()}: {msg.content}\n" for msg in reversed(previous_messages)]
    while entries and sum(len(entry) for entry in entries) > CHAT_HISTORY_CHAR_LIMIT:
        entries.pop(0)

    history_context = "".join(entries)
    return history_context, len(entries), len(history_context)


def _extract_markdown_report(result: dict) -> str:
    """
    Saved analysis uses the deal_chat skill in non-streaming mode. That path asks
    the model for JSON, while generated documents need markdown body text.
    """
    if not isinstance(result, dict):
        return str(result or "").strip()

    for key in ("report", "analyst_report", "content"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    parsed_json = result.get("parsed_json")
    if isinstance(parsed_json, dict):
        for key in ("report", "analyst_report", "content", "response"):
            value = parsed_json.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    raw_report = (result.get("response") or result.get("thinking") or "").strip()
    if not raw_report:
        return ""

    try:
        decoded = json.loads(raw_report)
    except Exception:
        return raw_report

    if isinstance(decoded, dict):
        for key in ("report", "analyst_report", "content", "response"):
            value = decoded.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return raw_report


def _deal_comparison_context(deal, selected_deal_ids: list | None = None) -> str:
    from deals.models import Deal

    selected_ids = [str(item) for item in (selected_deal_ids or []) if item]
    competitor_qs = Deal.objects.filter(id__in=selected_ids).exclude(id=deal.id).order_by("title")
    comparison_deals = [deal, *list(competitor_qs)]
    payload = []

    for item in comparison_deals:
        current_analysis = item.current_analysis if isinstance(item.current_analysis, dict) else {}
        canonical_snapshot = current_analysis.get("canonical_snapshot") if isinstance(current_analysis, dict) else {}
        deal_model_data = current_analysis.get("deal_model_data") if isinstance(current_analysis, dict) else {}
        report = (
            current_analysis.get("analyst_report")
            or canonical_snapshot.get("analyst_report")
            or item.deal_summary
            or ""
        )
        payload.append({
            "role": "current_deal" if item.id == deal.id else "selected_pipeline_competitor",
            "id": str(item.id),
            "title": item.title,
            "industry": item.industry,
            "sector": item.sector,
            "current_phase": item.current_phase,
            "priority": item.priority,
            "funding_ask": item.funding_ask,
            "funding_ask_for": item.funding_ask_for,
            "deal_summary": item.deal_summary,
            "deal_model_data": deal_model_data if isinstance(deal_model_data, dict) else {},
            "analysis_excerpt": report[:3000],
        })

    return json.dumps(payload, default=str, ensure_ascii=True, indent=2)


def _truncate_text(value: str | None, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    head = max(limit - 400, limit // 2)
    tail = max(limit - head - 40, 0)
    return f"{text[:head]}\n\n[...TRUNCATED...]\n\n{text[-tail:] if tail else ''}".strip()

@shared_task(bind=True)
def generate_chat_response_async(self, conversation_id: str, user_message: str, skill_name: str, metadata: dict, audit_log_id: str):
    """
    Background task to generate and save an AI chat response.
    Includes autoretry to handle DB commit race conditions.
    """
    try:
        conversation = AIConversation.objects.filter(id=conversation_id).first()
        audit_log = AIAuditLog.objects.filter(id=audit_log_id).first()
        if not conversation:
            logger.warning(
                "Chat task %s skipped: conversation %s no longer exists.",
                self.request.id,
                conversation_id,
            )
            if audit_log:
                audit_log.status = 'FAILED'
                audit_log.error_message = "Conversation no longer exists."
                audit_log.save(update_fields=['status', 'error_message'])
            return {"status": "error", "error": "conversation_not_found"}
        if not audit_log:
            logger.warning(
                "Chat task %s skipped: audit log %s no longer exists.",
                self.request.id,
                audit_log_id,
            )
            return {"status": "error", "error": "audit_log_not_found"}
        
        # Update log with worker info
        audit_log.celery_task_id = self.request.id
        audit_log.status = 'PROCESSING'
        audit_log.save(update_fields=['celery_task_id', 'status'])
        broadcast_audit_log_update(audit_log)

        # Update triggering user message with audit_log_id
        user_msg = AIMessage.objects.filter(
            conversation=conversation, 
            role='user'
        ).order_by('-created_at').first()
        if user_msg:
            if not isinstance(user_msg.applied_filters, dict):
                user_msg.applied_filters = {}
            user_msg.applied_filters['audit_log_id'] = audit_log_id
            user_msg.save(update_fields=['applied_filters'])

        ai_service = AIProcessorService()
        history_context, history_messages_used, history_chars_used = _build_history_context(conversation)
        
        if skill_name == 'universal_chat':
            chat_service = UniversalChatService(ai_service)
            task_metadata = chat_service.process_intent_and_build_metadata(
                user_message, conversation_id, history_context, audit_log_id
            )
            audit_log.source_metadata = {
                **(audit_log.source_metadata or {}),
                "used_query_builder": bool(task_metadata.get("used_query_builder", True)),
                "gate_mode": task_metadata.get("gate_mode", "fresh_retrieval"),
                "gate_reason": task_metadata.get("gate_reason"),
                "query_plan": task_metadata.get("query_plan") if task_metadata.get("used_query_builder", True) else None,
                "flow_version": task_metadata.get("flow_version"),
                "flow_config_id": task_metadata.get("flow_config_id"),
                "history_messages_used": history_messages_used,
                "history_chars_used": history_chars_used,
                "deals_considered": task_metadata.get("deals_considered"),
                "retrieved_chunk_count": task_metadata.get("retrieved_chunk_count"),
                "selected_chunk_count": task_metadata.get("selected_chunk_count"),
                "selected_sources": task_metadata.get("selected_sources"),
            }
            audit_log.save(update_fields=['source_metadata'])
            final_content = user_message
            if not AISkill.objects.filter(name='universal_chat').exists():
                final_content = (
                    "[CHAT HISTORY]\n"
                    f"{task_metadata.get('history_context', '')}\n\n"
                    "[RETRIEVED CONTEXT]\n"
                    f"{task_metadata.get('context_data', '')}\n\n"
                    "[USER QUERY]\n"
                    f"{user_message}"
                )
        elif skill_name == 'deal_chat':
            chat_service = UniversalChatService(ai_service)
            model_provider = (metadata or {}).get("model_provider", "vllm")
            if model_provider == "anthropic":
                from deals.models import Deal
                deal = Deal.objects.filter(id=metadata.get("deal_id")).first()
                deal_title = deal.title if deal else ""
                task_metadata = {
                    "model_provider": "anthropic",
                    "history_context": history_context,
                    "context_data": f"You are chatting about the deal: {deal_title}.",
                    "deal_context": f"You are chatting about the deal: {deal_title}.",
                    "audit_log_id": audit_log_id,
                    "query_plan": {"mode": "privacy_bypass", "user_query": user_message},
                    "answer_generation_prompt": "You are a secure Claude assistant. Public search is allowed but no private database access.",
                    "used_query_builder": False,
                    "gate_mode": "privacy_bypass",
                    "gate_reason": "RAG-bypass enforced for Claude model privacy.",
                    "deals_considered": 1 if deal_title else 0,
                    "retrieved_chunk_count": 0,
                    "selected_chunk_count": 0,
                    "selected_sources": [],
                }
            elif (metadata or {}).get("interactive_context_data"):
                task_metadata = {
                    "history_context": history_context,
                    "context_data": metadata.get("interactive_context_data"),
                    "deal_context": metadata.get("interactive_context_data"),
                    "audit_log_id": audit_log_id,
                    "query_plan": metadata.get("query_plan") or {},
                    "answer_generation_prompt": chat_service._stage_settings("answer_generation").get("prompt_template"),
                    "used_query_builder": True,
                    "gate_mode": "interactive_selection",
                    "gate_reason": "Analyst selected deals/documents/chunks before answer generation.",
                    "deals_considered": 0,
                    "retrieved_chunk_count": 0,
                    "selected_chunk_count": len(metadata.get("selected_sources") or []),
                    "selected_sources": metadata.get("selected_sources") or [],
                }
            else:
                task_metadata = chat_service.process_single_deal_build_metadata(
                    user_message,
                    conversation_id,
                    history_context,
                    audit_log_id,
                    metadata.get("deal_id"),
                )
            audit_log.source_metadata = {
                **(audit_log.source_metadata or {}),
                "used_query_builder": True,
                "gate_mode": task_metadata.get("gate_mode"),
                "gate_reason": task_metadata.get("gate_reason"),
                "query_plan": task_metadata.get("query_plan"),
                "flow_version": task_metadata.get("flow_version"),
                "flow_config_id": task_metadata.get("flow_config_id"),
                "history_messages_used": history_messages_used,
                "history_chars_used": history_chars_used,
                "deals_considered": task_metadata.get("deals_considered"),
                "retrieved_chunk_count": task_metadata.get("retrieved_chunk_count"),
                "selected_chunk_count": task_metadata.get("selected_chunk_count"),
                "selected_sources": task_metadata.get("selected_sources"),
            }
            audit_log.save(update_fields=['source_metadata'])
            final_content = user_message
        else:
            task_metadata = metadata or {}
            task_metadata['audit_log_id'] = audit_log_id
            task_metadata['history_context'] = history_context
            task_metadata.setdefault('_source_metadata', {})
            task_metadata['_source_metadata'] = {
                **(task_metadata.get('_source_metadata') or {}),
                "history_messages_used": history_messages_used,
                "history_chars_used": history_chars_used,
            }
            final_content = user_message

        if skill_name == 'deal_chat':
            task_metadata['audit_log_id'] = audit_log_id
            task_metadata['history_context'] = history_context
            task_metadata.setdefault('_source_metadata', {})
            task_metadata['_source_metadata'] = {
                **(task_metadata.get('_source_metadata') or {}),
                "history_messages_used": history_messages_used,
                "history_chars_used": history_chars_used,
                "retrieved_chunk_count": task_metadata.get("retrieved_chunk_count"),
                "selected_sources": task_metadata.get("selected_sources"),
            }

        task_metadata['model_provider'] = (metadata or {}).get('model_provider', 'vllm')
        if skill_name == 'deal_chat':
            task_metadata['personality_only_system'] = True
            task_metadata['prompt_template_override'] = DEAL_CHAT_CONVERSATIONAL_PROMPT

        full_text = ""
        full_thinking = ""
        last_save_time = time.time()
        last_stream_broadcast = 0.0
        pending_response_delta = ""
        pending_thinking_delta = ""

        # Call the AI service with stream=True
        for chunk_str in ai_service.process_content(
            content=final_content,
            skill_name=skill_name,
            source_type=skill_name,
            source_id=str(conversation.id),
            metadata=task_metadata,
            stream=True
        ):
            try:
                chunk = json.loads(chunk_str)
                response_delta = chunk.get("response", "")
                thinking_delta = chunk.get("thinking", "")
                full_text += response_delta
                full_thinking += thinking_delta
                pending_response_delta += response_delta
                pending_thinking_delta += thinking_delta
                now = time.time()
                # vLLM can emit token-sized chunks faster than React can paint.
                # Batch them briefly so the browser sees a smooth stream instead
                # of hundreds of synchronous websocket state updates.
                if now - last_stream_broadcast >= 0.2:
                    broadcast_ai_stream_delta(
                        audit_log,
                        response_delta=pending_response_delta,
                        thinking_delta=pending_thinking_delta,
                    )
                    pending_response_delta = ""
                    pending_thinking_delta = ""
                    last_stream_broadcast = now
                
                # Throttle DB updates to once per second to avoid lock contention
                if time.time() - last_save_time > 1.0:
                    audit_log.raw_response = full_text
                    audit_log.raw_thinking = full_thinking
                    audit_log.save(update_fields=['raw_response', 'raw_thinking'])
                    last_save_time = time.time()
            except (json.JSONDecodeError, KeyError, TypeError):
                pass

        broadcast_ai_stream_delta(
            audit_log,
            response_delta=pending_response_delta,
            thinking_delta=pending_thinking_delta,
        )
        full_text, full_thinking = _split_leaked_thinking(full_text, full_thinking)

        # Check if the stream layer already marked this as failed (fixes Bug 3 and Bug 9)
        audit_log.refresh_from_db(fields=['status', 'error_message'])
        if audit_log.status == 'FAILED':
            logger.warning(f"Stream failed for Conv {conversation_id}: {audit_log.error_message}")
            broadcast_audit_log_update(audit_log, event_type="terminal", done=True)
            return {"status": "error", "error": audit_log.error_message or "Stream failed"}

        if full_text:
            # Commit final message to conversation history
            AIMessage.objects.create(
                conversation=conversation,
                role='assistant',
                content=full_text,
                thinking=full_thinking,
                applied_filters={"audit_log_id": audit_log_id}
            )
            # Finalize Audit Log
            audit_log.raw_response = full_text
            audit_log.raw_thinking = full_thinking
            audit_log.status = 'COMPLETED'
            audit_log.is_success = True
            audit_log.save(update_fields=['raw_response', 'raw_thinking', 'status', 'is_success'])
            broadcast_audit_log_update(audit_log, event_type="terminal", done=True)
            
            logger.info(f"Background chat response generated for Conv: {conversation_id}")
            return {"status": "success", "message_length": len(full_text)}
        else:
            audit_log.status = 'FAILED'
            audit_log.error_message = "AI returned an empty response."
            audit_log.save()
            broadcast_audit_log_update(audit_log, event_type="terminal", done=True)
            return {"status": "error", "error": "Empty response"}

    except Exception as e:
        logger.error(f"Async chat response failed: {str(e)}")
        if 'audit_log' in locals():
            audit_log.status = 'FAILED'
            audit_log.error_message = str(e)
            audit_log.save()
            broadcast_audit_log_update(audit_log, event_type="terminal", done=True)
        raise e


@shared_task(bind=True)
def discover_documents_async(
    self,
    session_id: str,
    query_plan: dict,
    deal_ids: list[str],
    current_deal_id: str | None = None,
    audit_log_id: str | None = None,
):
    """
    Background task to discover and rerank documents for selected deals.
    """
    audit_log = AIAuditLog.objects.filter(id=audit_log_id).first() if audit_log_id else None
    try:
        if audit_log:
            audit_log.status = 'PROCESSING'
            audit_log.celery_task_id = self.request.id
            audit_log.save(update_fields=['status', 'celery_task_id'])
            broadcast_audit_log_update(audit_log)

        from ai_orchestrator.services.ai_processor import AIProcessorService
        from ai_orchestrator.services.universal_chat import UniversalChatService

        service = UniversalChatService(AIProcessorService())
        documents, diagnostics = service.documents_for_selected_deals(
            plan=query_plan,
            deal_ids=deal_ids,
            current_deal_id=current_deal_id
        )
        
        if audit_log:
            audit_log.status = 'COMPLETED'
            audit_log.is_success = True
            audit_log.parsed_json = {
                "documents": documents,
                "diagnostics": diagnostics,
                "session_id": session_id,
            }
            audit_log.save(update_fields=['status', 'is_success', 'parsed_json'])
            broadcast_audit_log_update(audit_log, done=True)

        return {
            "status": "success",
            "documents": documents,
            "diagnostics": diagnostics,
            "session_id": session_id,
        }
    except Exception as e:
        logger.error(f"Async document discovery failed for session {session_id}: {str(e)}", exc_info=True)
        if audit_log:
            audit_log.status = 'FAILED'
            audit_log.error_message = str(e)
            audit_log.is_success = False
            audit_log.save(update_fields=['status', 'error_message', 'is_success'])
            broadcast_audit_log_update(audit_log, done=True)
        return {"status": "error", "error": str(e)}


@shared_task(bind=True)
def discover_chunks_async(
    self,
    session_id: str,
    query_plan: dict,
    document_ids: list[str],
    current_deal_id: str | None = None,
    is_multi_deal: bool = False,
    audit_log_id: str | None = None,
):
    """
    Background task to discover relevant evidence chunks for selected documents.
    """
    audit_log = AIAuditLog.objects.filter(id=audit_log_id).first() if audit_log_id else None
    try:
        if audit_log:
            audit_log.status = 'PROCESSING'
            audit_log.celery_task_id = self.request.id
            audit_log.save(update_fields=['status', 'celery_task_id'])
            broadcast_audit_log_update(audit_log)

        from ai_orchestrator.services.ai_processor import AIProcessorService
        from ai_orchestrator.services.universal_chat import UniversalChatService

        service = UniversalChatService(AIProcessorService())
        if is_multi_deal:
            chunks, diagnostics = service.chunks_for_selected_documents_multi_deal(
                plan=query_plan,
                document_ids=document_ids,
                current_deal_id=current_deal_id,
            )
        else:
            chunks, diagnostics = service.chunks_for_selected_documents(
                plan=query_plan,
                deal_id=current_deal_id,
                document_ids=document_ids,
            )

        from django.core.cache import cache
        session_key = f"deal_helper_session:{session_id}"
        session_payload = cache.get(session_key)
        if isinstance(session_payload, dict):
            session_payload.update({
                "selected_document_ids": document_ids,
                "candidate_chunks": chunks,
                "chunk_diagnostics": diagnostics,
            })
            cache.set(session_key, session_payload, timeout=60 * 60 * 4)
            
        if audit_log:
            audit_log.status = 'COMPLETED'
            audit_log.is_success = True
            audit_log.parsed_json = {
                "chunks": chunks,
                "diagnostics": diagnostics,
                "session_id": session_id,
            }
            audit_log.save(update_fields=['status', 'is_success', 'parsed_json'])
            broadcast_audit_log_update(audit_log, done=True)

        return {
            "status": "success",
            "chunks": chunks,
            "diagnostics": diagnostics,
            "session_id": session_id,
        }
    except Exception as e:
        logger.error(f"Async chunk discovery failed for session {session_id}: {str(e)}", exc_info=True)
        if audit_log:
            audit_log.status = 'FAILED'
            audit_log.error_message = str(e)
            audit_log.is_success = False
            audit_log.save(update_fields=['status', 'error_message', 'is_success'])
            broadcast_audit_log_update(audit_log, done=True)
        return {"status": "error", "error": str(e)}


@shared_task(bind=True)
def generate_deal_helper_analysis_async(
    self,
    deal_id: str,
    directive: str,
    mode: str,
    audit_log_id: str,
    document_title: str = "",
    generated_document_id: str | None = None,
    selected_context: str = "",
    selected_deal_ids: list | None = None,
    selected_document_ids: list | None = None,
    selected_chunk_ids: list | None = None,
    model_provider: str = "vllm",
):
    try:
        from deals.models import Deal, DealGeneratedDocument, DealDocument

        deal = Deal.objects.get(id=deal_id)
        if mode == "full_rewrite":
            raise ValueError("Full rewrite is no longer supported from deal helper.")
        audit_log = AIAuditLog.objects.filter(id=audit_log_id).first()
        if audit_log:
            audit_log.status = "PROCESSING"
            audit_log.celery_task_id = self.request.id
            audit_log.save(update_fields=["status", "celery_task_id"])

        ai_service = AIProcessorService()
        chat_service = UniversalChatService(ai_service)
        saved_context = chat_service._saved_relationship_context_for_deal(deal)
        deal_specific_prompt = (deal.analysis_prompt or "").strip()
        current_deal_documents = list(deal.documents.all().order_by("-created_at")[:80])
        selected_documents = list(
            DealDocument.objects.filter(id__in=[str(item) for item in (selected_document_ids or []) if item])
            .select_related("deal")
            .order_by("deal__title", "-created_at")
        )
        documents = (selected_documents if selected_documents else current_deal_documents)[:50]
        document_evidence = [
            {
                "document_name": doc.title,
                "document_type": doc.document_type,
                "deal": doc.deal.title if doc.deal else deal.title,
                "document_summary": _truncate_text(doc.normalized_text or doc.extracted_text, 2500),
                "claims": [],
                "metrics": [],
                "citations": [doc.title] if doc.title else [],
                "normalized_text": _truncate_text(doc.normalized_text or doc.extracted_text, 2500),
                "source_map": {
                    "document_id": str(doc.id),
                    "document_name": doc.title,
                    "deal": doc.deal.title if doc.deal else deal.title,
                },
            }
            for doc in documents
            if (doc.normalized_text or doc.extracted_text)
        ]
        supporting_raw_chunks = [
            {
                "source_title": doc.title,
                "deal": doc.deal.title if doc.deal else deal.title,
                "text": _truncate_text(doc.normalized_text or doc.extracted_text, 1500),
            }
            for doc in documents[:30]
            if (doc.normalized_text or doc.extracted_text)
        ]
        if selected_context:
            supporting_raw_chunks.insert(0, {
                "source_title": "Deal helper selected evidence",
                "deal": deal.title,
                "text": _truncate_text(selected_context, 24000),
            })
        selected_pipeline_context = _deal_comparison_context(deal, selected_deal_ids)
        skill_name = "deal_helper_directive_document"
        prompt = (
            f"Create generated document '{document_title or directive[:80] or 'Directive Document'}' "
            f"for deal: {deal.title}.\n\n"
            f"Analyst directive:\n{directive}\n\n"
            "Follow the directive's requested artifact type, structure, and format. "
            "Use only the selected evidence and supplied deal context for factual claims."
        )
        output_mode = "directive_document"
        if not AISkill.objects.filter(name=skill_name).exists():
            skill_name = None
            prompt = (
                "Create a generated deal document in Markdown.\n\n"
                "Return only the final Markdown document. Do not return JSON, markdown fences, "
                "prompt instructions, or hidden reasoning. Follow the analyst directive as the "
                "primary structure and format. Do not force the canonical deal synthesis 7-section "
                "structure unless the directive explicitly asks for it.\n\n"
                f"[DOCUMENT TITLE]\n{document_title or directive[:80] or 'Directive Document'}\n\n"
                f"[DEAL]\n{deal.title}\n\n"
                f"[ANALYST DIRECTIVE]\n{directive}\n\n"
                f"[DOCUMENT EVIDENCE JSON]\n{json.dumps(document_evidence, default=str, ensure_ascii=True)}\n\n"
                f"[SUPPORTING RAW CHUNKS JSON]\n{json.dumps(supporting_raw_chunks, default=str, ensure_ascii=True)}\n\n"
                f"[STORED COMPETITOR / RELATED-DEAL CONTEXT]\n{saved_context or 'No stored competitor or related-deal context.'}\n\n"
                f"[SELECTED PIPELINE CONTEXT]\n{selected_pipeline_context}\n\n"
                f"[SELECTED DEAL HELPER CONTEXT]\n{_truncate_text(selected_context, 24000) if selected_context else 'No manually selected evidence context supplied.'}"
            )
        result = ai_service.process_content(
            content=prompt,
            skill_name=skill_name,
            source_type="deal_helper_analysis",
            source_id=str(deal.id),
            metadata={
                "audit_log_id": str(audit_log.id) if audit_log else audit_log_id,
                "model_provider": model_provider,
                "mode": mode,
                "directive": directive,
                "document_title": document_title or directive[:80] or "Directive Document",
                "document_evidence_json": json.dumps(document_evidence, default=str, ensure_ascii=True),
                "supporting_raw_chunks_json": json.dumps(supporting_raw_chunks, default=str, ensure_ascii=True),
                "output_mode": output_mode,
                "deal_title": deal.title,
                "deal_baseline_json": json.dumps({
                    "title": deal.title,
                    "sector": deal.sector or "N/A",
                    "industry": deal.industry or "N/A",
                    "funding_ask": deal.funding_ask or "N/A",
                    "funding_ask_for": deal.funding_ask_for or "N/A",
                }, default=str, ensure_ascii=True),
                "deal_specific_prompt": deal_specific_prompt or "No deal-specific prompt saved.",
                "related_deal_context": saved_context or "No stored competitor or related-deal context.",
                "selected_pipeline_context": selected_pipeline_context,
                "selected_context": _truncate_text(selected_context, 24000) if selected_context else "No manually selected evidence context supplied.",
                "response_mode": "markdown",
                "chat_template_kwargs": {"enable_thinking": False},
                "max_tokens": 4096,
            },
        )
        if isinstance(result, dict) and result.get("error"):
            raise ValueError(f"deal_synthesis failed: {result.get('error')}")
        if isinstance(result, dict) and isinstance(result.get("parsed_json"), dict):
            analysis = result["parsed_json"]
            clean_thinking = result.get("thinking") or analysis.get("thinking") or ""
        else:
            analysis = result if isinstance(result, dict) else {}
            clean_thinking = analysis.get("thinking", "") if isinstance(analysis, dict) else ""
        report = _extract_markdown_report(analysis)
        if not report:
            report = _extract_markdown_report(result if isinstance(result, dict) else {"response": ""})
        if not report:
            raise ValueError("deal_synthesis did not return an analyst_report.")
        if not isinstance(analysis, dict):
            analysis = {}
        analysis.setdefault("analyst_report", report)
        metadata = analysis.setdefault("metadata", {})
        metadata.update({
            "mode": mode,
            "directive": directive,
            "deal_specific_prompt": deal_specific_prompt,
            "documents_analyzed": [doc.title for doc in documents],
            "analysis_input_files": [{"file_id": str(doc.id), "file_name": doc.title} for doc in documents],
            "selected_deal_ids": selected_deal_ids or [],
            "selected_document_ids": selected_document_ids or [],
            "selected_chunk_ids": selected_chunk_ids or [],
        })
        next_version = None
        generated_document = DealGeneratedDocument.objects.filter(id=generated_document_id).first() if generated_document_id else None
        if generated_document:
            generated_document.title = (document_title or generated_document.title or directive[:80] or "Directive Document")[:255]
            generated_document.directive = directive
            generated_document.content = report
            generated_document.selected_deal_ids = selected_deal_ids or []
            generated_document.selected_document_ids = selected_document_ids or []
            generated_document.selected_chunk_ids = selected_chunk_ids or []
            generated_document.audit_log_id = audit_log_id
            generated_document.save(update_fields=[
                "title", "directive", "content", "selected_deal_ids",
                "selected_document_ids", "selected_chunk_ids", "audit_log_id",
            ])
        if audit_log:
            audit_log.raw_response = report
            audit_log.raw_thinking = clean_thinking
            audit_log.status = "COMPLETED"
            audit_log.is_success = True
            audit_log.save(update_fields=["raw_response", "raw_thinking", "status", "is_success"])
            broadcast_audit_log_update(audit_log, done=True)
        return {"status": "success", "version": next_version, "generated_document_id": generated_document_id}
    except Exception as e:
        audit_log = AIAuditLog.objects.filter(id=audit_log_id).first()
        if audit_log:
            audit_log.status = "FAILED"
            audit_log.error_message = str(e)
            audit_log.is_success = False
            audit_log.save(update_fields=["status", "error_message", "is_success"])
            broadcast_audit_log_update(audit_log, done=True)
        logger.error("Deal helper analysis failed: %s", e, exc_info=True)
        raise e


@shared_task(bind=True)
def check_local_ai_connection_task(self):
    """
    Background task to check local AI connection and update cache.
    """
    from django.core.cache import cache
    from .services.ai_processor import AIProcessorService
    from .services.vm_service import VMControlService
    import time
    import logging

    logger = logging.getLogger(__name__)
    logger.info("Executing background local AI connection probe...")

    ai_service = AIProcessorService()
    vm_service = VMControlService()

    vm_online = False
    available_models = []
    vm_status = "unknown"

    try:
        vm_status = vm_service.get_status()
    except Exception as e:
        logger.warning("Failed to check VM status: %s", e)

    try:
        vm_online = ai_service.provider.health_check()
        if vm_online:
            available_models = ai_service.provider.get_available_models()
    except Exception as e:
        logger.warning("vLLM connectivity probe failed: %s", e)

    telemetry = {
        "loaded_models": [{"name": m, "vram_gb": "unknown"} for m in available_models]
    }

    result = {
        "vm_online": vm_online,
        "vm_status": vm_status,
        "available_models": available_models,
        "telemetry": telemetry,
        "status": "completed",
        "checked_at": time.time()
    }

    cache.set("local_ai_connection_status", result, timeout=300)
    logger.info("Background local AI connection probe completed: %s", result)
    return result
