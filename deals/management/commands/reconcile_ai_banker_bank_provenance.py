from django.core.management.base import BaseCommand
from django.db import transaction

from deals.models import Deal, DealFieldProvenance
from deals.services.field_provenance import serializable_field_value


class Command(BaseCommand):
    help = "Keep AI banker relationships paired with Sheet or AI bank provenance."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")

    @transaction.atomic
    def handle(self, *args, **options):
        latest = {}
        for record in DealFieldProvenance.objects.order_by("deal_id", "field_name", "-created_at", "-id"):
            latest.setdefault((record.deal_id, record.field_name), record)

        records = []
        deals_checked = 0
        for deal in Deal.objects.select_related("bank"):
            banker_source = latest.get((deal.id, "primary_contact"))
            if not banker_source or banker_source.source_type != DealFieldProvenance.SourceType.AI:
                continue
            deals_checked += 1
            if deal.bank_name:
                field_name, value = "bank_name", deal.bank_name
            elif deal.legacy_investment_bank:
                field_name, value = "legacy_investment_bank", deal.legacy_investment_bank
            elif deal.bank_id:
                field_name, value = "bank", deal.bank
            else:
                continue
            current = latest.get((deal.id, field_name))
            if current and current.source_type in {
                DealFieldProvenance.SourceType.SHEET,
                DealFieldProvenance.SourceType.AI,
            }:
                continue
            records.append(DealFieldProvenance(
                deal=deal,
                field_name=field_name,
                source_type=DealFieldProvenance.SourceType.AI,
                source_id=f"ai-relationship:{banker_source.source_id or banker_source.id}",
                previous_value=serializable_field_value(value),
                value=serializable_field_value(value),
            ))
        if options["apply"]:
            DealFieldProvenance.objects.bulk_create(records)
        else:
            transaction.set_rollback(True)
        action = "created" if options["apply"] else "would create"
        self.stdout.write(f"Checked {deals_checked} AI banker relationships; {action} {len(records)} paired bank provenance records.")
