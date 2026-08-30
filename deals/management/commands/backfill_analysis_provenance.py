from django.core.management.base import BaseCommand
from django.db import transaction

from deals.models import Deal, DealFieldProvenance
from deals.services.field_provenance import TRACKED_DEAL_FIELDS, serializable_field_value


AI_DERIVED_FIELDS = TRACKED_DEAL_FIELDS - {"title", "source_onedrive_id"}


class Command(BaseCommand):
    help = "Attribute otherwise untracked fields on analyzed deals to AI."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")

    @transaction.atomic
    def handle(self, *args, **options):
        existing = set(DealFieldProvenance.objects.values_list("deal_id", "field_name"))
        records = []
        deals_checked = 0
        queryset = Deal.objects.filter(analyses__isnull=False).distinct().prefetch_related("responsibility", "analyses")
        for deal in queryset:
            deals_checked += 1
            latest_analysis = max(deal.analyses.all(), key=lambda item: (item.version, item.created_at))
            for field_name in AI_DERIVED_FIELDS:
                if (deal.id, field_name) in existing:
                    continue
                if field_name == "responsibility":
                    value = sorted(str(profile.id) for profile in deal.responsibility.all())
                else:
                    value = getattr(deal, field_name, None)
                serialized = serializable_field_value(value)
                if serialized in (None, "", [], {}):
                    continue
                records.append(DealFieldProvenance(
                    deal=deal,
                    field_name=field_name,
                    source_type=DealFieldProvenance.SourceType.AI,
                    source_id=f"deal-analysis:{latest_analysis.id}:fallback",
                    previous_value=None,
                    value=serialized,
                ))
        if options["apply"]:
            DealFieldProvenance.objects.bulk_create(records)
        else:
            transaction.set_rollback(True)
        action = "created" if options["apply"] else "would create"
        self.stdout.write(f"Checked {deals_checked} analyzed deals; {action} {len(records)} AI provenance records.")
