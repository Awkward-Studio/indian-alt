import logging
import json
import time
import re
from celery import shared_task
from .models import AIAuditLog, AIMessage, AIConversation, AIPersonality, AISkill
from .services.ai_processor import AIProcessorService
from .services.universal_chat import UniversalChatService

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
