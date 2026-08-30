from django.core.management.base import BaseCommand
from django.db import transaction

from deals.models import Deal, DealFieldProvenance
from deals.services.field_provenance import TRACKED_DEAL_FIELDS, serializable_field_value

ONEDRIVE_FIELDS = {"title", "source_onedrive_id"}


class Command(BaseCommand):
    help = "Attribute previously untracked fields on source-backed deals to OneDrive."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")

    @transaction.atomic
    def handle(self, *args, **options):
        existing = set(DealFieldProvenance.objects.values_list("deal_id", "field_name"))
        records = []
        deals_checked = 0
        queryset = Deal.objects.exclude(source_onedrive_id__isnull=True).exclude(source_onedrive_id="").prefetch_related("responsibility")
        for deal in queryset:
            deals_checked += 1
            for field_name in TRACKED_DEAL_FIELDS:
                if field_name not in ONEDRIVE_FIELDS:
                    continue
                key = (deal.id, field_name)
                if key in existing:
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
                    source_type=DealFieldProvenance.SourceType.ONEDRIVE,
                    source_id=f"onedrive:{deal.source_onedrive_id}",
                    previous_value=None,
                    value=serialized,
                ))
        if options["apply"]:
            DealFieldProvenance.objects.bulk_create(records)
        else:
            transaction.set_rollback(True)
        action = "created" if options["apply"] else "would create"
        self.stdout.write(f"Checked {deals_checked} OneDrive-backed deals; {action} {len(records)} provenance records.")
