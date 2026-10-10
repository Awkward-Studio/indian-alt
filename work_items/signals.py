import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from deals.models import DealAnalysis
from ai_orchestrator.models import AIAuditLog


logger = logging.getLogger(__name__)


@receiver(post_save, sender=AIAuditLog)
def synchronize_saved_report_gap_suggestions(sender, instance, **kwargs):
    metadata = instance.source_metadata or {}
    if instance.source_type != 'vdr_report_section' or instance.status != 'COMPLETED' or metadata.get('report_section_outcome') not in {'accepted', 'saved_with_gaps'}:
        return
    parent_id = metadata.get('vdr_parent_audit_id')
    if not parent_id:
        return
    deal_id = AIAuditLog.objects.filter(pk=parent_id).values_list('source_id', flat=True).first()
    if not deal_id:
        return

    def synchronize():
        try:
            from .services import sync_latest_deal_suggestions
            sync_latest_deal_suggestions(deal_id)
        except Exception:
            logger.exception('Failed to synchronize report-gap suggestions for deal %s', deal_id)

    transaction.on_commit(synchronize)


@receiver(post_save, sender=DealAnalysis)
def synchronize_analysis_task_suggestions(sender, instance, **kwargs):
    deal_id = instance.deal_id

    def synchronize():
        try:
            from .services import sync_latest_deal_suggestions
            sync_latest_deal_suggestions(deal_id)
        except Exception:
            logger.exception("Failed to synchronize task suggestions for deal %s", deal_id)

    transaction.on_commit(synchronize)
