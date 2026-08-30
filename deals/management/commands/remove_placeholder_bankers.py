from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q

from banks.models import Bank
from contacts.models import Contact
from deals.models import Deal, DealFieldProvenance
from deals.services.field_provenance import record_deal_field_changes


PLACEHOLDERS = (
    "evidence unavailable",
    "external diligence",
    "not available",
    "unknown",
)


def placeholder_query(field):
    query = Q()
    for value in PLACEHOLDERS:
        query |= Q(**{f"{field}__icontains": value})
    return query


class Command(BaseCommand):
    help = "Remove placeholder bank/contact values and links from deals."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")

    @transaction.atomic
    def handle(self, *args, **options):
        placeholder_contacts = Contact.objects.filter(placeholder_query("name") | placeholder_query("email")).distinct()
        placeholder_banks = Bank.objects.filter(placeholder_query("name")).distinct()
        contact_ids = list(placeholder_contacts.values_list("id", flat=True))
        bank_ids = list(placeholder_banks.values_list("id", flat=True))

        primary_deals = list(Deal.objects.filter(primary_contact_id__in=contact_ids))
        bank_deals = list(Deal.objects.filter(bank_id__in=bank_ids))
        raw_deals = list(Deal.objects.filter(
            placeholder_query("legacy_investment_bank")
            | placeholder_query("bank_name")
            | placeholder_query("primary_contact_name")
        ).distinct())

        if options["apply"]:
            for deal in primary_deals:
                previous = deal.primary_contact
                deal.primary_contact = None
                deal.save(update_fields=["primary_contact"])
                record_deal_field_changes(
                    deal, {"primary_contact": (previous, None)},
                    source_type=DealFieldProvenance.SourceType.HUMAN,
                    source_id="cleanup:placeholder-contact",
                )
            for deal in bank_deals:
                previous = deal.bank
                deal.bank = None
                deal.save(update_fields=["bank"])
                record_deal_field_changes(
                    deal, {"bank": (previous, None)},
                    source_type=DealFieldProvenance.SourceType.HUMAN,
                    source_id="cleanup:placeholder-bank",
                )
            for deal in raw_deals:
                changes = {}
                for field in ("legacy_investment_bank", "bank_name", "primary_contact_name"):
                    value = getattr(deal, field)
                    if value and any(marker in value.casefold() for marker in PLACEHOLDERS):
                        changes[field] = (value, None)
                        setattr(deal, field, None)
                if changes:
                    deal.save(update_fields=list(changes))
                    record_deal_field_changes(
                        deal, changes,
                        source_type=DealFieldProvenance.SourceType.HUMAN,
                        source_id="cleanup:placeholder-text",
                    )
            for deal in Deal.objects.filter(additional_contacts__id__in=contact_ids).distinct():
                deal.additional_contacts.remove(*contact_ids)
            placeholder_contacts.delete()
            placeholder_banks.delete()
        else:
            transaction.set_rollback(True)

        action = "Removed" if options["apply"] else "Would remove"
        self.stdout.write(
            f"{action} {len(contact_ids)} placeholder contacts from {len(primary_deals)} primary links, "
            f"{len(bank_ids)} placeholder banks from {len(bank_deals)} bank links, and placeholder text from {len(raw_deals)} deals."
        )
