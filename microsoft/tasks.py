import logging
from celery import shared_task
from .models import Email
from ai_orchestrator.services.document_processor import DocumentProcessorService
from ai_orchestrator.services.ai_processor import AIProcessorService
from ai_orchestrator.models import AIAuditLog, AIPersonality, AISkill
from ai_orchestrator.services.runtime import AIRuntimeService
from .services.graph_service import GraphAPIService

logger = logging.getLogger(__name__)

@shared_task(bind=True)
def analyze_email_async(self, email_id: str, audit_log_id: str | None = None):
    """
    Initial traversal of an email and its attachments.
    Returns a 'file tree' of attachments to allow for user selection, matching the folder flow.
    """
    logger.info(f"Starting async email traversal for {email_id}")
    from ai_orchestrator.services.realtime import log_worker_event
    
    try:
        email = Email.objects.get(id=email_id)
    except Email.DoesNotExist:
        logger.error(f"Email {email_id} not found")
        return {"error": "Email not found"}

    personality = AIPersonality.objects.filter(is_default=True).first()
    skill = AISkill.objects.filter(name='deal_extraction').first()
    default_model = AIRuntimeService.get_text_model(personality)

    if audit_log_id:
        try:
            audit_log = AIAuditLog.objects.get(id=audit_log_id)
            log_worker_event(audit_log, f"Worker picking up traversal for email {email.subject}", status='PROCESSING')
            audit_log.celery_task_id = self.request.id
            audit_log.save(update_fields=['status', 'celery_task_id'])
        except AIAuditLog.DoesNotExist:
            audit_log_id = None

    if not audit_log_id:
        audit_log = AIRuntimeService.create_audit_log(
            source_type='email',
            source_id=email_id,
            context_label=f"Email: {email.subject}",
            personality=personality,
            skill=skill,
            status='PROCESSING',
            is_success=False,
            model_used=default_model,
            system_prompt="Initializing forensic email traversal...",
            user_prompt=f"Analyzing email signal: {email.subject}",
            celery_task_id=self.request.id,
        )
        log_worker_event(audit_log, f"Initialized traversal log for email {email.subject}")

    try:
        attachments = email.attachments if isinstance(email.attachments, list) else []
        file_tree = []
        
        # Virtual file for the email body itself
        file_tree.append({
            "id": "body",
            "name": "Email Body",
            "type": "file",
            "size": len(email.body_text or email.body_preview or ""),
            "mimeType": "text/plain",
            "is_body": True
        })

        for att in attachments:
            file_tree.append({
                "id": att.get("id"),
                "name": att.get("name"),
                "type": "file",
                "size": att.get("size", 0),
                "mimeType": att.get("contentType"),
                "is_body": False
            })

        log_worker_event(audit_log, f"Traversal complete. Discovered {len(file_tree)} objects in email.", status='COMPLETED', done=True)
        
        audit_log.system_prompt = f"Traversal complete. Found {len(file_tree)} objects in email. Awaiting selection..."
        audit_log.is_success = True
        audit_log.source_metadata = {
            "file_tree": file_tree,
            "email_id": email_id,
            "subject": email.subject,
            "total_files": len(file_tree),
            "workflow_stage": "traversal_complete",
            "interaction_status": "pending",
            "interaction_mode": "editable",
        }
        audit_log.save()

        return {
            "status": "success",
            "phase": "traversal",
            "audit_log_id": str(audit_log.id),
            "email_id": email_id,
            "total_files": len(file_tree),
            "file_tree": file_tree,
            "source_type": "email"
        }
        
    except Exception as e:
        log_worker_event(audit_log, f"Traversal error: {str(e)}", status='FAILED', done=True)
        raise e
