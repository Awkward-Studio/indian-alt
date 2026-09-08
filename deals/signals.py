"""Signals for the deals app."""
import logging
from django.core.exceptions import ValidationError
from django.db.models.signals import pre_delete
from django.dispatch import receiver

from .models import Deal

logger = logging.getLogger(__name__)


@receiver(pre_delete, sender=Deal)
def reset_connected_emails_on_deal_delete(sender, instance, **kwargs):
    """
    When a Deal is deleted, detach connected emails, reset their processing state,
    and remove associated ingestion runs so they can be re-processed from scratch.
    """
    from microsoft.models import Email, EmailIngestionRun, EmailEvidenceLink
    from ai_orchestrator.models import DocumentChunk

    deal_id_str = str(instance.id)

    emails = {e.id: e for e in instance.emails.all()}

    if instance.source_email_id:
        try:
            for email in Email.objects.filter(id=instance.source_email_id):
                emails[email.id] = email
        except (ValueError, ValidationError):
            pass
        for email in Email.objects.filter(graph_id=instance.source_email_id):
            emails[email.id] = email

    for run in EmailIngestionRun.objects.filter(match__deal_id=deal_id_str).select_related('email'):
        if run.email_id:
            emails[run.email_id] = run.email

    for run in EmailIngestionRun.objects.filter(match__suggested_deal_id=deal_id_str).select_related('email'):
        if run.email_id:
            emails[run.email_id] = run.email

    for link in EmailEvidenceLink.objects.filter(deal=instance).select_related('meeting_note'):
        if link.meeting_note_id and link.meeting_note:
            note = link.meeting_note
            note.deals.remove(instance)
            if not note.deals.exists():
                DocumentChunk.objects.filter(source_type='meeting_note', source_id=str(note.id)).delete()
                note.delete()

    email_objs = list(emails.values())
    if not email_objs:
        return

    email_ids = [e.id for e in email_objs]
    email_id_strs = [str(eid) for eid in email_ids]

    EmailIngestionRun.objects.filter(email_id__in=email_ids).delete()

    DocumentChunk.objects.filter(
        source_type__in=['email', 'attachment'],
        source_id__in=email_id_strs,
    ).delete()

    for email in email_objs:
        email.deal = None
        email.is_processed = False
        email.is_indexed = False
        email.processing_status = 'idle'
        email.analysis_result = {}
        email.processing_error = None
        email.processed_at = None
        email.save(update_fields=[
            'deal',
            'is_processed',
            'is_indexed',
            'processing_status',
            'analysis_result',
            'processing_error',
            'processed_at',
        ])

    logger.info(
        'Reset %d email(s) upon deletion of deal %s (%s)',
        len(email_objs),
        deal_id_str,
        instance.title,
    )
