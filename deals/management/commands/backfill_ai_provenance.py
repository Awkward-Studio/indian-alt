from django.core.management.base import BaseCommand
from django.db import transaction

from deals.models import DealAnalysis, DealFieldProvenance
from deals.services.deal_creation import DealCreationService
from deals.services.field_provenance import serializable_field_value


FIELD_MAPPING = {
    "title": "title",
    "industry": "industry",
    "sector": "sector",
    "funding_ask": "funding_ask",
    "funding_ask_for": "funding_ask_for",
    "deal_summary": "deal_summary",
    "company_details": "company_details",
    "priority_rationale": "priority_rationale",
    "priority": "priority",
    "city": "city",
    "state": "state",
    "country": "country",
    "themes": "themes",
    "is_female_led": "is_female_led",
    "bank_name": "bank_name",
    "primary_contact_name": "primary_contact_name",
}


def same_value(database_value, analysis_value):
    if isinstance(database_value, bool):
        normalized = str(analysis_value).strip().casefold() in {"true", "yes", "1", "on"}
        return database_value == normalized
    return str(database_value or "").strip() == str(analysis_value or "").strip()


class Command(BaseCommand):
    help = "Backfill missing AI provenance when stored analysis values exactly match deal fields."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")

    @transaction.atomic
    def handle(self, *args, **options):
        existing = set(DealFieldProvenance.objects.values_list("deal_id", "field_name"))
        queued = set()
        records = []
        analyses_checked = exact_matches = 0

        analyses = DealAnalysis.objects.select_related("deal", "deal__bank", "deal__primary_contact").order_by("-version", "-created_at")
        for analysis in analyses:
            analyses_checked += 1
            payload = analysis.analysis_json if isinstance(analysis.analysis_json, dict) else {}
            model_data = DealCreationService._get_analysis_model_data(payload)
            deal = analysis.deal
            candidates = {model_field: model_data.get(analysis_field) for analysis_field, model_field in FIELD_MAPPING.items()}

            relationships = payload.get("source_relationships") or (payload.get("portable_deal_data") or {}).get("source_relationships") or {}
            bank_data = relationships.get("bank") if isinstance(relationships, dict) else None
            if isinstance(bank_data, dict) and deal.bank_id and bank_data.get("name") == deal.bank.name:
                candidates["bank"] = deal.bank
            contact_data = relationships.get("primary_contact") if isinstance(relationships, dict) else None
            if isinstance(contact_data, dict) and deal.primary_contact_id:
                name_matches = contact_data.get("name") and str(contact_data["name"]).strip() == str(deal.primary_contact.name or "").strip()
                email_matches = contact_data.get("email") and str(contact_data["email"]).strip().casefold() == str(deal.primary_contact.email or "").strip().casefold()
                if name_matches or email_matches:
                    candidates["primary_contact"] = deal.primary_contact

            for field_name, analysis_value in candidates.items():
                key = (deal.id, field_name)
                if analysis_value in (None, "") or key in existing or key in queued:
                    continue
                database_value = getattr(deal, field_name)
                if not same_value(database_value, analysis_value):
                    continue
                exact_matches += 1
                queued.add(key)
                records.append(DealFieldProvenance(
                    deal=deal,
                    field_name=field_name,
                    source_type=DealFieldProvenance.SourceType.AI,
                    source_id=f"deal-analysis:{analysis.id}:v{analysis.version}",
                    previous_value=None,
                    value=serializable_field_value(database_value),
                ))

        if options["apply"]:
            DealFieldProvenance.objects.bulk_create(records)
        else:
            transaction.set_rollback(True)
        action = "created" if options["apply"] else "would create"
        self.stdout.write(f"Checked {analyses_checked} analyses; found {exact_matches} exact field matches; {action} {len(records)} AI provenance records.")
