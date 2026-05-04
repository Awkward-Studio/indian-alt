import logging
import json
import time
import re
from celery import shared_task
from .models import AIAuditLog, AIMessage, AIConversation, AIPersonality, AISkill
from .services.ai_processor import AIProcessorService
from .services.universal_chat import UniversalChatService
from .services.runtime import AIRuntimeService

from .services.realtime import broadcast_audit_log_update

logger = logging.getLogger(__name__)

CHAT_HISTORY_MESSAGE_LIMIT = 3
CHAT_HISTORY_CHAR_LIMIT = 12000


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
            if (metadata or {}).get("interactive_context_data"):
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

        full_text = ""
        full_thinking = ""
        last_save_time = time.time()

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
                full_text += chunk.get("response", "")
                full_thinking += chunk.get("thinking", "")
                
                # Throttle DB updates to once per second to avoid lock contention
                if time.time() - last_save_time > 1.0:
                    audit_log.raw_response = full_text
                    audit_log.raw_thinking = full_thinking
                    audit_log.save(update_fields=['raw_response', 'raw_thinking'])
                    last_save_time = time.time()
            except:
                pass

        full_text, full_thinking = _split_leaked_thinking(full_text, full_thinking)

        if full_text:
            # Commit final message to conversation history
            AIMessage.objects.create(
                conversation=conversation,
                role='assistant',
                content=full_text,
                thinking=full_thinking
            )
            # Finalize Audit Log
            audit_log.raw_response = full_text
            audit_log.raw_thinking = full_thinking
            audit_log.status = 'COMPLETED'
            audit_log.is_success = True
            audit_log.save(update_fields=['raw_response', 'raw_thinking', 'status', 'is_success'])
            
            logger.info(f"Background chat response generated for Conv: {conversation_id}")
            return {"status": "success", "message_length": len(full_text)}
        else:
            audit_log.status = 'FAILED'
            audit_log.error_message = "AI returned an empty response."
            audit_log.save()
            return {"status": "error", "error": "Empty response"}

    except Exception as e:
        logger.error(f"Async chat response failed: {str(e)}")
        if 'audit_log' in locals():
            audit_log.status = 'FAILED'
            audit_log.error_message = str(e)
            audit_log.save()
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
):
    try:
        from deals.models import Deal, DealAnalysis, AnalysisKind, DealGeneratedDocument, DealDocument

        deal = Deal.objects.get(id=deal_id)
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
        documents = selected_documents if selected_documents else current_deal_documents
        document_evidence = [
            {
                "document_name": doc.title,
                "document_type": doc.document_type,
                "deal": doc.deal.title if doc.deal else deal.title,
                "document_summary": (doc.normalized_text or doc.extracted_text or "")[:3000],
                "claims": [],
                "metrics": [],
                "citations": [doc.title] if doc.title else [],
                "normalized_text": (doc.normalized_text or doc.extracted_text or "")[:3000],
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
                "text": (doc.normalized_text or doc.extracted_text or "")[:1800],
            }
            for doc in documents[:30]
            if (doc.normalized_text or doc.extracted_text)
        ]
        if selected_context:
            supporting_raw_chunks.insert(0, {
                "source_title": "Deal helper selected evidence",
                "deal": deal.title,
                "text": selected_context[:12000],
            })
        selected_pipeline_context = _deal_comparison_context(deal, selected_deal_ids)
        current_analysis = deal.current_analysis if isinstance(deal.current_analysis, dict) else {}
        current_report = current_analysis.get("report") or deal.deal_summary or ""
        task_label = "full rewrite" if mode == "full_rewrite" else "user-directed addendum"
        prompt = (
            f"Create a saved {task_label} for deal: {deal.title}.\n\n"
            "Current deal baseline metadata:\n"
            f"- Title: {deal.title}\n"
            f"- Sector: {deal.sector or 'N/A'}\n"
            f"- Industry: {deal.industry or 'N/A'}\n"
            f"- Funding ask: {deal.funding_ask or 'N/A'}\n"
            f"- Current deal summary excerpt: {(deal.deal_summary or current_report or 'No current analysis available.')[:1600]}\n\n"
            f"User directive:\n{directive}\n\n"
            "For a full rewrite, use the deal_synthesis 7-section analyst_report structure as the default document structure. "
            "For a directive document, satisfy the user directive while retaining the same evidence discipline and financial-analysis standards.\n\n"
            "If the directive asks for a competitor or pipeline comparison, compare the current deal directly against the selected_pipeline_competitor records below. "
            "Build a concrete comparison table inside analyst_report with one row per current/competitor deal and columns for available financial metrics, funding ask, revenue/ARR/GMV, EBITDA/PAT/margins, valuation, leverage/debt, capex, growth, and key financial risks. "
            "Use N/A where a metric is missing; do not substitute generic industry benchmarks as competitors. "
            "After the table, add concise notes on which competitors have insufficient financial evidence and the exact missing items to request.\n\n"
            f"Selected pipeline comparison set:\n{selected_pipeline_context}\n\n"
            f"Deal metadata:\nIndustry: {deal.industry or 'N/A'}\nSector: {deal.sector or 'N/A'}\nFunding ask: {deal.funding_ask or 'N/A'}\n\n"
            "Prefer primary selected evidence over existing deal summaries because the user explicitly selected it for this document."
        )
        result = ai_service.process_content(
            content=prompt,
            skill_name="deal_synthesis",
            source_type="deal_helper_analysis",
            source_id=str(deal.id),
            metadata={
                "audit_log_id": str(audit_log.id) if audit_log else audit_log_id,
                "mode": mode,
                "document_evidence_json": json.dumps(document_evidence, default=str, ensure_ascii=True),
                "supporting_raw_chunks_json": json.dumps(supporting_raw_chunks, default=str, ensure_ascii=True),
                "deal_specific_prompt": deal_specific_prompt or "No deal-specific prompt saved.",
                "related_deal_context": saved_context or "No stored competitor or related-deal context.",
                "selected_context": selected_context[:16000] if selected_context else "No manually selected evidence context supplied.",
                "chat_template_kwargs": {"enable_thinking": False},
            },
        )
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
        if mode == "full_rewrite":
            next_version = (deal.analyses.order_by("-version").values_list("version", flat=True).first() or 0) + 1
            DealAnalysis.objects.create(
                deal=deal,
                version=next_version,
                analysis_kind=AnalysisKind.SUPPLEMENTAL,
                thinking=clean_thinking,
                ambiguities=metadata.get("ambiguous_points", []),
                analysis_json=analysis,
            )
        else:
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
        return {"status": "success", "version": next_version, "generated_document_id": generated_document_id}
    except Exception as e:
        audit_log = AIAuditLog.objects.filter(id=audit_log_id).first()
        if audit_log:
            audit_log.status = "FAILED"
            audit_log.error_message = str(e)
            audit_log.is_success = False
            audit_log.save(update_fields=["status", "error_message", "is_success"])
        logger.error("Deal helper analysis failed: %s", e, exc_info=True)
        raise e
