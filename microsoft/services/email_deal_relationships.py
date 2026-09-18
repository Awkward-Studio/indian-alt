"""Deterministic people and firm links for email-created deals."""

from django.db import transaction

from accounts.models import Profile
from contacts.models import Contact
from deals.models import DealFieldProvenance
from deals.services.contact_linking import sync_deal_contact_links
from deals.services.field_provenance import record_deal_field_changes
from microsoft.models import Email
from microsoft.services.email_thread_originator import EmailThreadOriginatorResolver


class EmailDealRelationshipService:
    @staticmethod
    def _thread(email: Email) -> list[Email]:
        queryset = Email.objects.filter(id=email.id)
        if email.conversation_id:
            queryset = Email.objects.filter(
                email_account_id=email.email_account_id,
                conversation_id=email.conversation_id,
            )
        return list(queryset.select_related("email_account"))

    @staticmethod
    def forwarding_profile(email: Email):
        sender = str(email.from_email or "").strip()
        if not sender:
            return None
        return Profile.objects.filter(email__iexact=sender, is_disabled=False).first()

    @classmethod
    @transaction.atomic
    def assign_ia_team(cls, deal, email: Email, *, fallback_user=None, explicit_profile=None):
        profile = explicit_profile or cls.forwarding_profile(email)
        if profile is None:
            candidate = getattr(fallback_user, "profile", None)
            if candidate and not candidate.is_disabled:
                profile = candidate
        if profile is None:
            return None

        previous = list(deal.responsibility.all())
        if len(previous) == 1 and previous[0].id == profile.id:
            return profile
        deal.responsibility.set([profile])
        record_deal_field_changes(
            deal,
            {"responsibility": (previous, [profile])},
            source_type=DealFieldProvenance.SourceType.HUMAN,
            source_id=f"email:{email.id}:forwarder",
            changed_by=fallback_user,
        )
        return profile

    @classmethod
    @transaction.atomic
    def link_primary_contact(cls, deal, email: Email, *, overwrite=False):
        if deal.primary_contact_id and not overwrite:
            return deal.primary_contact
        originator = EmailThreadOriginatorResolver.resolve(cls._thread(email))
        if not originator:
            return None

        contact = Contact.objects.filter(email__iexact=originator.address).first()
        if not contact:
            contact = Contact.objects.create(
                name=originator.name or originator.address.split("@", 1)[0],
                email=originator.address,
                designation="Auto-created from originating email",
                contact_type=Contact.ContactType.BANKER,
            )
        elif originator.name and not contact.name:
            contact.name = originator.name
            contact.save(update_fields=["name"])

        previous_contact = deal.primary_contact
        previous_contact_name = deal.primary_contact_name
        previous_bank = deal.bank
        deal.primary_contact = contact
        if contact.bank_id:
            deal.bank = contact.bank
            if not deal.bank_name:
                deal.bank_name = contact.bank.name
        if not deal.primary_contact_name:
            deal.primary_contact_name = contact.name
        update_fields = ["primary_contact", "primary_contact_name"]
        if contact.bank_id:
            update_fields.extend(["bank", "bank_name"])
        deal.save(update_fields=list(dict.fromkeys(update_fields)))
        sync_deal_contact_links(deal, primary_contact=contact, primary_contact_provided=True)
        if previous_contact_id := getattr(previous_contact, "id", None):
            contact_changed = previous_contact_id != contact.id
        else:
            contact_changed = True
        if contact_changed:
            contact.source_count += 1
            contact.save(update_fields=["source_count"])
        record_deal_field_changes(
            deal,
            {
                "primary_contact": (previous_contact, contact),
                "primary_contact_name": (previous_contact_name, contact.name),
                "bank": (previous_bank, deal.bank),
            },
            source_type=DealFieldProvenance.SourceType.AI,
            source_id=f"email:{email.id}:{originator.evidence}",
        )
        return contact
