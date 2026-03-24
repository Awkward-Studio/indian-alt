import logging
import json
import time
from celery import shared_task
from .models import AIAuditLog, AIMessage, AIConversation, AIPersonality, AISkill
from .services.ai_processor import AIProcessorService
from .services.universal_chat import UniversalChatService

logger = logging.getLogger(__name__)

@shared_task(bind=True, autoretry_for=(AIConversation.DoesNotExist, AIAuditLog.DoesNotExist), retry_backoff=True, max_retries=3)
def generate_chat_response_async(self, conversation_id: str, user_message: str, skill_name: str, metadata: dict, audit_log_id: str):
    """
    Background task to generate and save an AI chat response.
    Includes autoretry to handle DB commit race conditions.
    """
    try:
        conversation = AIConversation.objects.get(id=conversation_id)
        audit_log = AIAuditLog.objects.get(id=audit_log_id)
        
        # Update log with worker info
        audit_log.celery_task_id = self.request.id
        audit_log.status = 'PROCESSING'
        audit_log.save(update_fields=['celery_task_id', 'status'])

        ai_service = AIProcessorService()
        history_context = ""
        
        # --- RETRIEVE CONVERSATION HISTORY ---
        previous_messages = AIMessage.objects.filter(conversation=conversation).order_by('-created_at')[1:11] # Skip the current user message
        for msg in reversed(previous_messages):
            history_context += f"{msg.role.upper()}: {msg.content}\n"
        # -------------------------------------
        
        if skill_name == 'universal_chat':
            chat_service = UniversalChatService(ai_service)
            task_metadata = chat_service.process_intent_and_build_metadata(
                user_message, conversation_id, history_context, audit_log_id
            )
        else:
            task_metadata = metadata or {}
            task_metadata['audit_log_id'] = audit_log_id
            task_metadata['history_context'] = history_context

        full_text = ""
        full_thinking = ""
        last_save_time = time.time()

        # Call the AI service with stream=True
        for chunk_str in ai_service.process_content(
            content=user_message,
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
