from __future__ import annotations

from typing import Iterable

from django.db import transaction
from django.db.models import Q

from contacts.models import Contact
from deals.models import Deal


def _normalize_contact_ids(values: Iterable) -> list[str]:
    normalized: list[str] = []
    for value in values or []:
        if value is None:
            continue
        text = str(value).strip()
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def sync_deal_contact_links(
    deal: Deal,
    *,
    primary_contact: Contact | None = None,
    primary_contact_provided: bool = False,
    additional_contacts: Iterable[Contact] | None = None,
    additional_contacts_provided: bool = False,
) -> Deal:
    update_fields: list[str] = []

    if primary_contact_provided:
        deal.primary_contact = primary_contact
        if primary_contact and primary_contact.bank:
            deal.bank = primary_contact.bank
            update_fields.append("bank")
        update_fields.append("primary_contact")

    if update_fields:
        deal.save(update_fields=list(dict.fromkeys(update_fields)))

    if additional_contacts_provided:
        contacts = list(additional_contacts or [])
        if primary_contact_provided and primary_contact is not None:
            contacts = [contact for contact in contacts if contact.id != primary_contact.id]
        elif deal.primary_contact_id:
            contacts = [contact for contact in contacts if contact.id != deal.primary_contact_id]

        deal.additional_contacts.set(contacts)
        synced_ids = [str(contact.id) for contact in contacts]
        if list(deal.other_contacts or []) != synced_ids:
            deal.other_contacts = synced_ids
            deal.save(update_fields=["other_contacts"])
    else:
        contact_ids = _normalize_contact_ids(contact.id for contact in deal.additional_contacts.all())
        if list(deal.other_contacts or []) != contact_ids:
            deal.other_contacts = contact_ids
            deal.save(update_fields=["other_contacts"])

    return deal


@transaction.atomic
def sync_contact_deal_links(contact: Contact, links: Iterable[dict]) -> None:
    desired_links = {}
    for link in links or []:
        deal_id = str(link.get("deal_id") or "").strip()
        if not deal_id:
            continue
        desired_links[deal_id] = bool(link.get("is_primary"))

    current_deals = Deal.objects.filter(
        Q(primary_contact=contact) | Q(additional_contacts=contact)
    ).distinct()

    desired_deals = {
        str(deal.id): deal
        for deal in Deal.objects.filter(id__in=list(desired_links.keys()))
    }

    for deal in current_deals:
        wanted_primary = desired_links.get(str(deal.id))
        if wanted_primary is None:
            changed = False
            if deal.primary_contact_id == contact.id:
                deal.primary_contact = None
                deal.save(update_fields=["primary_contact"])
                changed = True
            if deal.additional_contacts.filter(id=contact.id).exists():
                deal.additional_contacts.remove(contact)
                changed = True
            if changed:
                sync_deal_contact_links(deal)
            continue

        if wanted_primary:
            deal.primary_contact = contact
            if contact.bank:
                deal.bank = contact.bank
                deal.save(update_fields=["primary_contact", "bank"])
            else:
                deal.save(update_fields=["primary_contact"])
            deal.additional_contacts.remove(contact)
        else:
            if deal.primary_contact_id == contact.id:
                deal.primary_contact = None
                deal.save(update_fields=["primary_contact"])
            deal.additional_contacts.add(contact)

        sync_deal_contact_links(deal)

    for deal_id, deal in desired_deals.items():
        if current_deals.filter(id=deal.id).exists():
            continue
        if desired_links[deal_id]:
            deal.primary_contact = contact
            if contact.bank:
                deal.bank = contact.bank
                deal.save(update_fields=["primary_contact", "bank"])
            else:
                deal.save(update_fields=["primary_contact"])
            deal.additional_contacts.remove(contact)
        else:
            deal.additional_contacts.add(contact)
        sync_deal_contact_links(deal)


def sync_primary_contact_bank(contact: Contact) -> None:
    for deal in Deal.objects.filter(primary_contact=contact).select_related("bank", "primary_contact"):
        if deal.bank_id != contact.bank_id:
            deal.bank = contact.bank
            deal.save(update_fields=["bank"])
