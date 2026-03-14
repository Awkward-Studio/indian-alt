import logging
from celery import shared_task
from .models import Email
from ai_orchestrator.services.document_processor import DocumentProcessorService
from ai_orchestrator.services.ai_processor import AIProcessorService
from ai_orchestrator.models import AIAuditLog, AIPersonality, AISkill
from .services.graph_service import GraphAPIService

logger = logging.getLogger(__name__)

@shared_task(bind=True)
def analyze_email_async(self, email_id: str):
    """
    Asynchronous analysis of an email and its attachments.
    """
    logger.info(f"Starting async email analysis for {email_id}")
    
    try:
        email = Email.objects.get(id=email_id)
    except Email.DoesNotExist:
        logger.error(f"Email {email_id} not found")
        return {"error": "Email not found"}

    # Create immediate visibility in the AI History Ledger
    personality = AIPersonality.objects.filter(is_default=True).first()
    skill = AISkill.objects.filter(name='deal_extraction').first()
    
    audit_log = AIAuditLog.objects.create(
        source_type='email',
        source_id=email_id,
        context_label=f"Email: {email.subject}",
        personality=personality,
        skill=skill,
        status='PROCESSING',
        is_success=False,
        model_used='qwen3.5:latest',
        system_prompt="Initializing forensic email analysis...",
        user_prompt=f"Analyzing email signal: {email.subject}",
        celery_task_id=self.request.id
    )

    doc_processor = DocumentProcessorService()
    ai_service = AIProcessorService()
    graph_service = GraphAPIService()
    
    try:
        # 1. Prepare Content
        combined_text = f"SUBJECT: {email.subject}\nFROM: {email.from_email}\nBODY:\n{email.body_text or email.body_preview}"
        all_images = []
        
        # 2. Extract from attachments
        attachments = email.attachments if isinstance(email.attachments, list) else []
        for att in attachments:
            try:
                # Use application permissions to get attachment content via Graph
                att_content = graph_service.get_attachment_content(
                    email.email_account.email, 
                    email.graph_id, 
                    att['id']
                )
                
                if 'contentBytes' in att_content:
                    import base64
                    content = base64.b64decode(att_content['contentBytes'])
                    
                    # Extract Text
                    text = doc_processor.extract_text(content, att['name'])
                    combined_text += f"\n\n--- ATTACHMENT: {att['name']} ---\n{text[:5000]}"
                    
                    # Extract Visuals for GLM-OCR
                    visuals = doc_processor.extract_visuals(content, att['name'])
                    if visuals:
                        all_images.extend(visuals)
            except Exception as e:
                logger.error(f"Error processing attachment {att.get('name')}: {e}")

        # 3. AI Analysis
        meta = {
            '_source_metadata': {
                "email_id": email_id,
                "subject": email.subject,
                "has_attachments": len(attachments) > 0
            },
            'audit_log_id': str(audit_log.id)
        }
        
        result = ai_service.process_content(
            content=combined_text,
            skill_name="deal_extraction",
            source_type="email",
            images=all_images,
            metadata=meta
        )
        
        # Update email as processed
        email.is_processed = True
        email.save(update_fields=['is_processed'])

        return {"status": "success", "email_id": email_id}
        
    except Exception as e:
        audit_log.status = 'FAILED'
        audit_log.error_message = str(e)
        audit_log.save()
        raise e
