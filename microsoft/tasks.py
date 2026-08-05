import logging
from celery import shared_task, chord
from .models import Email
from ai_orchestrator.services.document_processor import DocumentProcessorService
from ai_orchestrator.services.ai_processor import AIProcessorService
from ai_orchestrator.models import AIAuditLog, AIPersonality, AISkill
from ai_orchestrator.services.runtime import AIRuntimeService
from .services.graph_service import GraphAPIService
from deals.services.email_intelligence import EmailIntelligenceService
from .services.email_thread_unfolder import EmailThreadUnfolder
from .services.email_thread_router import EmailThreadRouter, EmailThreadRouteMode

logger = logging.getLogger(__name__)

@shared_task(bind=True)
def analyze_email_async(self, email_id: str, audit_log_id: str | None = None):
    """
    Autonomous thread-aware analysis of an email.
    1. Extracts proposed deal metadata (routing).
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

    # 1. Audit Log Setup
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
            context_label=f"Email: {email.subject}",
            personality=personality,
            skill=skill,
            status='PROCESSING',
            is_success=False,
            model_used=default_model,
            system_prompt="Initializing forensic thread analysis swarm...",
            user_prompt=f"Analyzing email signal: {email.subject}",
            celery_task_id=self.request.id,
        )

    # 2. Skip Standalone Routing Pass (Handled during synthesis)
    # Improved fallback: Strip common prefixes to find the actual company name
    raw_subject = email.subject or ""
    clean_name = raw_subject.replace("Fw:", "").replace("Re:", "").replace("Investment Opportunity", "").replace("Project", "").replace("|", "").strip()
    # Strip common descriptive suffixes often found in subjects
    clean_name = clean_name.split("–")[0].split("-")[0].strip()
    
    proposed_intel = {
        "company_name": clean_name or "New Deal Analysis",
    }

    try:
        # 3. Aggregate FULL Thread History
        if email.conversation_id:
            thread_emails = list(Email.objects.filter(conversation_id=email.conversation_id))
        else:
            thread_emails = [email]
        route = EmailThreadRouter.resolve(thread_emails)
        route_payload = route.as_dict()
        if route.mode == EmailThreadRouteMode.BLOCKED_AMBIGUOUS:
            audit_log.source_metadata = {
                **(audit_log.source_metadata or {}),
                "email_id": email_id,
                "route": route_payload,
                "workflow_stage": "routing_blocked",
                "interaction_status": "blocked",
            }
            audit_log.error_message = route.error
            audit_log.save(update_fields=["source_metadata", "error_message"])
            log_worker_event(audit_log, route.error, status="FAILED", done=True)
            return {
                "status": "blocked",
                "phase": "routing",
                "audit_log_id": str(audit_log.id),
                "route": route_payload,
                "error": route.error,
            }

        thread_deltas = EmailThreadUnfolder.unfold(thread_emails)
        emails_by_id = {str(item.id): item for item in thread_emails}
        email_count = len(thread_emails)
        log_worker_event(audit_log, f"Thread discovery complete: Found {email_count} messages in this conversation.", status='PROCESSING')
        
        file_tree = []
        user_email = email.email_account.email

        # Track seen attachments to avoid duplicates across the thread
        seen_attachment_ids = set()

        for delta in thread_deltas:
            e = emails_by_id[delta.email_id]
            # Add each Body as a separate analysis object
            # Empty deltas are exact repeats/empty bodies and must not be analyzed twice.
            if delta.text:
                file_tree.append({
                    "id": f"body_{e.id}",
                    "name": f"Email Body - {e.created_at.strftime('%Y-%m-%d %H:%M')}",
                    "email_id": str(e.id),
                    "type": "file",
                    "is_body": True,
                    "real_body_id": "body",
                    "body_delta": delta.text,
                    "delta_diagnostics": delta.as_dict(),
                })
            
            # Add Attachments from this specific message
            attachments = e.attachments if isinstance(e.attachments, list) else []
            for att in attachments:
                att_id = att.get("id")
                if att_id not in seen_attachment_ids:
                    file_tree.append({
                        "id": att_id,
                        "name": att.get("name"),
                        "email_id": str(e.id),
                        "type": "file",
                        "is_body": False
                    })
                    seen_attachment_ids.add(att_id)

        # 4. Trigger Parallel Extraction Swarm
        log_worker_event(audit_log, f"Queueing {len(file_tree)} objects (bodies + attachments) for parallel analysis...")
        
        tasks = [
            process_single_thread_document_async.s(file, route.deal_id, user_email, str(audit_log.id))
            for file in file_tree
        ]
        callback = finalize_thread_analysis_async.s(route.deal_id, str(audit_log.id))
        
        # Prepare tracking IDs
        _, _, child_task_ids, callback_task_id = _prepare_vdr_task_ids(tasks, callback)
        
        audit_log.source_metadata = {
            "file_tree": file_tree,
            "email_id": email_id,
            "route": route_payload,
            "thread_stats": {
                "message_count": email_count,
                "oldest_msg": min(e.created_at for e in thread_emails).isoformat(),
                "latest_msg": max(e.created_at for e in thread_emails).isoformat(),
                "subjects": list(dict.fromkeys(e.subject for e in thread_emails)),
                "body_deltas": [delta.as_dict() for delta in thread_deltas],
            },
            "proposed_intel": proposed_intel,
            "child_task_ids": child_task_ids,
            "callback_task_id": callback_task_id,
            "workflow_stage": "analysis_pending",
            "interaction_status": "pending",
            "interaction_mode": "editable",
        }
        audit_log.save(update_fields=['source_metadata'])
        
        chord(tasks)(callback)

        return {
            "status": "queued",
            "phase": "analysis",
            "audit_log_id": str(audit_log.id),
            "total_objects": len(file_tree),
            "route": route_payload,
        }
        
    except Exception as e:
        log_worker_event(audit_log, f"Thread analysis error: {str(e)}", status='FAILED', done=True)
        raise e
