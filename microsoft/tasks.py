import logging
from celery import shared_task, chord
from .models import Email
from ai_orchestrator.services.document_processor import DocumentProcessorService
from ai_orchestrator.services.ai_processor import AIProcessorService
from ai_orchestrator.models import AIAuditLog, AIPersonality, AISkill
from ai_orchestrator.services.runtime import AIRuntimeService
from .services.graph_service import GraphAPIService
from deals.services.email_intelligence import EmailIntelligenceService

logger = logging.getLogger(__name__)

@shared_task(bind=True)
def analyze_email_async(self, email_id: str, audit_log_id: str | None = None):
    """
    Autonomous thread-aware analysis of an email.
    1. Resolves thread to a Deal.
    2. Builds a file tree for the entire thread.
    3. Triggers parallel vLLM extraction & normalization.
    """
    logger.info(f"Starting async autonomous email analysis for {email_id}")
    from ai_orchestrator.services.realtime import log_worker_event
    from deals.tasks import process_single_thread_document_async, finalize_thread_analysis_async, _prepare_vdr_task_ids
    
    try:
        email = Email.objects.get(id=email_id)
    except Email.DoesNotExist:
        logger.error(f"Email {email_id} not found")
        return {"error": "Email not found"}

    # 1. Thread Resolution & Routing
    log_worker_event(None, f"Resolving thread intelligence for email: {email.subject}")
    deal, created = EmailIntelligenceService.resolve_thread_to_deal(email_id)
    log_worker_event(None, f"Thread resolved to deal: {deal.title} ({'New' if created else 'Existing'})")

    # 2. Audit Log Setup
    personality = AIPersonality.objects.filter(is_default=True).first()
    skill = AISkill.objects.filter(name='deal_extraction').first()
    default_model = AIRuntimeService.get_text_model(personality)

    if audit_log_id:
        try:
            audit_log = AIAuditLog.objects.get(id=audit_log_id)
            audit_log.celery_task_id = self.request.id
            audit_log.save(update_fields=['status', 'celery_task_id'])
        except AIAuditLog.DoesNotExist:
            audit_log_id = None

    if not audit_log_id:
        audit_log = AIRuntimeService.create_audit_log(
            source_type='email',
            source_id=email_id,
            context_label=f"Thread: {deal.title}",
            personality=personality,
            skill=skill,
            status='PROCESSING',
            is_success=False,
            model_used=default_model,
            system_prompt="Initializing forensic thread analysis swarm...",
            user_prompt=f"Analyzing thread signal for deal: {deal.title}",
            celery_task_id=self.request.id,
        )

    try:
        # 3. Aggregate Thread Objects (Bodies + Attachments)
        thread_emails = Email.objects.filter(conversation_id=email.conversation_id).order_by('created_at')
        file_tree = []
        user_email = email.email_account.email

        for e in thread_emails:
            # Add Body
            file_tree.append({
                "id": "body",
                "name": f"Email Body - {e.created_at.strftime('%Y-%m-%d %H:%M')}",
                "email_id": str(e.id),
                "type": "file",
                "is_body": True
            })
            
            # Add Attachments
            attachments = e.attachments if isinstance(e.attachments, list) else []
            for att in attachments:
                file_tree.append({
                    "id": att.get("id"),
                    "name": att.get("name"),
                    "email_id": str(e.id),
                    "type": "file",
                    "is_body": False
                })

        # 4. Trigger Parallel Extraction Swarm
        log_worker_event(audit_log, f"Queueing {len(file_tree)} thread objects for parallel vLLM analysis...")
        
        tasks = [
            process_single_thread_document_async.s(file, str(deal.id), user_email, str(audit_log.id))
            for file in file_tree
        ]
        callback = finalize_thread_analysis_async.s(str(deal.id), str(audit_log.id))
        
        # Prepare tracking IDs
        _, _, child_task_ids, callback_task_id = _prepare_vdr_task_ids(tasks, callback)
        
        audit_log.source_metadata = {
            "file_tree": file_tree,
            "email_id": email_id,
            "deal_id": str(deal.id),
            "deal_title": deal.title,
            "child_task_ids": child_task_ids,
            "callback_task_id": callback_task_id,
            "workflow_stage": "analysis_pending",
            "interaction_status": "completed",
            "interaction_mode": "read_only",
        }
        audit_log.save(update_fields=['source_metadata'])
        
        chord(tasks)(callback)

        return {
            "status": "queued",
            "phase": "analysis",
            "audit_log_id": str(audit_log.id),
            "deal_id": str(deal.id),
            "total_objects": len(file_tree)
        }
        
    except Exception as e:
        log_worker_event(audit_log, f"Thread analysis error: {str(e)}", status='FAILED', done=True)
        raise e

