from __future__ import annotations

from django.db import transaction

from deals.models import Deal, DealReceiptDateSuggestion


class ReceiptDateEvidenceService:
    """Create idempotent suggestions only from inspectable source evidence."""

    @staticmethod
    @transaction.atomic
    def propose(*, deal: Deal, proposed_date, source_type: str, source_id: str, evidence: dict, confidence: float = 1.0):
        if not proposed_date or not source_id or not evidence:
            raise ValueError('A proposed date, source identifier, and evidence are required.')
        if source_type not in DealReceiptDateSuggestion.SourceType.values:
            raise ValueError('Unsupported receipt-date evidence source type.')
        suggestion, created = DealReceiptDateSuggestion.objects.get_or_create(
            deal=deal,
            proposed_date=proposed_date,
            source_type=source_type,
            source_id=source_id,
            defaults={'evidence': evidence, 'confidence': confidence},
        )
        if not created and suggestion.status in {
            DealReceiptDateSuggestion.Status.ACCEPTED,
            DealReceiptDateSuggestion.Status.REJECTED,
        }:
            return suggestion, False
        active = DealReceiptDateSuggestion.objects.select_for_update().filter(
            deal=deal,
            status__in=[
                DealReceiptDateSuggestion.Status.PENDING,
                DealReceiptDateSuggestion.Status.CONFLICT,
            ],
        )
        target_status = (
            DealReceiptDateSuggestion.Status.CONFLICT
            if active.values('proposed_date').distinct().count() > 1
            else DealReceiptDateSuggestion.Status.PENDING
        )
        active.update(status=target_status)
        suggestion.refresh_from_db()
        return suggestion, created

    @classmethod
    def propose_from_linked_email(cls, deal: Deal):
        if not deal.source_email_id:
            return None, False
        from microsoft.models import Email

        email = Email.objects.filter(graph_id=deal.source_email_id).first()
        if email is None or email.date_received is None:
            return None, False
        return cls.propose(
            deal=deal,
            proposed_date=email.date_received.date(),
            source_type=DealReceiptDateSuggestion.SourceType.SOURCE_EMAIL,
            source_id=email.graph_id,
            evidence={
                'email_id': str(email.id),
                'graph_id': email.graph_id,
                'subject': email.subject or '',
                'date_received': email.date_received.isoformat(),
            },
            confidence=1.0,
        )
