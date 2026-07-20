import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from deals.models import DealAnalysis


logger = logging.getLogger(__name__)


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
