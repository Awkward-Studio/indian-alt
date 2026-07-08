from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from django.db import transaction

from banks.models import Bank
from contacts.models import Contact
from deals.models import Deal
from deals.services.contact_linking import sync_deal_contact_links, sync_primary_contact_bank


NULL_MARKERS = {
    "",
    "na",
    "n/a",
    "none",
    "not available",
    "not identified",
    "not provided",
    "not specified",
    "null",
    "unknown",
}


def clean_text(value) -> str:
    if value is None:
        return ""
    text = " ".join(str(value).strip().split())
    return "" if text.lower() in NULL_MARKERS else text


def normalize_bank_name(value) -> str:
    text = clean_text(value).lower()
    text = re.sub(
        r"\b(private|pvt|limited|ltd|llp|plc|inc|corp|corporation|company|co)\b",
        " ",
        text,
    )
    text = re.sub(
        r"\b(advisors?|advisory|capital|securities|investment banking|investments?|bank|financial services|finance)\b",
        " ",
        text,
    )
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def normalize_contact_name(value) -> str:
    text = clean_text(value).lower()
    text = re.sub(r"\b(mr|mrs|ms|dr|ca)\.?\s+", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def merge_text_value(base: str | None, incoming: str | None) -> str | None:
    base = clean_text(base)
    incoming = clean_text(incoming)
    if not incoming:
        return base or None
    if not base:
        return incoming
    if incoming.lower() == base.lower() or incoming in base:
        return base
    if base in incoming:
        return incoming
    return f"{base}\n\n{incoming}"


def merge_list_value(base, incoming):
    merged = []
    for value in list(base or []) + list(incoming or []):
        if value not in merged:
            merged.append(value)
    return merged


@dataclass
class DedupeGroup:
    key: str
    items: list
    match_type: str


@dataclass
class RetentionRecommendation:
    score: int
    confidence: str
    reasons: list[str]


def _bank_score(bank: Bank) -> tuple:
    return (
        -bank.deals.count(),
        -bank.contacts.count(),
        0 if clean_text(bank.website_domain) else 1,
        0 if clean_text(bank.description) else 1,
        bank.created_at.isoformat() if bank.created_at else "",
        str(bank.id),
    )


def _contact_score(contact: Contact) -> tuple:
    primary_count = contact.primary_deals.count()
    additional_count = contact.additional_deals.count()
    meeting_count = contact.meetings.count()
    return (
        -(primary_count + additional_count + meeting_count),
        -int(contact.source_count or 0),
        0 if clean_text(contact.email) else 1,
        0 if clean_text(contact.designation) else 1,
        0 if clean_text(contact.linkedin_url) else 1,
        contact.created_at.isoformat() if contact.created_at else "",
        str(contact.id),
    )


def _confidence_from_margin(best_score: int, next_score: int | None, *, strong_threshold: int = 4) -> str:
    if next_score is None:
        return "high"
    margin = best_score - next_score
    if margin >= strong_threshold:
        return "high"
    if margin >= 2:
        return "medium"
    return "low"


def bank_retention_recommendations(items: Iterable[Bank]) -> dict[str, RetentionRecommendation]:
    scored = []
    for bank in items:
        reasons = []
        score = 0

        deal_count = bank.deals.count()
        contact_count = bank.contacts.count()
        if deal_count:
            score += min(8, deal_count)
            reasons.append(f"{deal_count} linked deal(s)")
        if contact_count:
            score += min(6, contact_count)
            reasons.append(f"{contact_count} linked contact(s)")
        if clean_text(bank.website_domain):
            score += 3
            reasons.append("has website domain")
        if clean_text(bank.description):
            score += 1
            reasons.append("has description")
        if clean_text(bank.name):
            score += 1
            reasons.append("has name")

        scored.append((bank, score, reasons))

    ordered_scores = sorted((score for _bank, score, _reasons in scored), reverse=True)
    best = ordered_scores[0] if ordered_scores else 0
    next_best = ordered_scores[1] if len(ordered_scores) > 1 else None
    best_confidence = _confidence_from_margin(best, next_best)

    recommendations = {}
    for bank, score, reasons in scored:
        confidence = best_confidence if score == best else "duplicate"
        recommendations[str(bank.id)] = RetentionRecommendation(
            score=score,
            confidence=confidence,
            reasons=reasons or ["no strong retention signal"],
        )
    return recommendations


def contact_retention_recommendations(items: Iterable[Contact]) -> dict[str, RetentionRecommendation]:
    scored = []
    for contact in items:
        reasons = []
        score = 0

        primary_count = contact.primary_deals.count()
        additional_count = contact.additional_deals.count()
        meeting_count = contact.meetings.count()
        relationship_count = primary_count + additional_count + meeting_count
        if primary_count:
            score += min(10, primary_count * 2)
            reasons.append(f"{primary_count} primary deal(s)")
        if additional_count:
            score += min(6, additional_count)
            reasons.append(f"{additional_count} additional deal(s)")
        if meeting_count:
            score += min(5, meeting_count)
            reasons.append(f"{meeting_count} meeting(s)")
        if relationship_count == 0:
            reasons.append("no linked relationships")
        if clean_text(contact.email):
            score += 4
            reasons.append("has email")
        if contact.bank_id:
            score += 2
            reasons.append("has bank")
        if clean_text(contact.designation):
            score += 1
            reasons.append("has designation")
        if clean_text(contact.linkedin_url):
            score += 1
            reasons.append("has linkedin")
        if int(contact.source_count or 0):
            score += min(4, int(contact.source_count or 0))
            reasons.append(f"source_count={contact.source_count}")
        if clean_text(contact.name):
            score += 1
            reasons.append("has name")

        scored.append((contact, score, reasons))

    ordered_scores = sorted((score for _contact, score, _reasons in scored), reverse=True)
    best = ordered_scores[0] if ordered_scores else 0
    next_best = ordered_scores[1] if len(ordered_scores) > 1 else None
    best_confidence = _confidence_from_margin(best, next_best)

    recommendations = {}
    for contact, score, reasons in scored:
        confidence = best_confidence if score == best else "duplicate"
        recommendations[str(contact.id)] = RetentionRecommendation(
            score=score,
            confidence=confidence,
            reasons=reasons or ["no strong retention signal"],
        )
    return recommendations


def format_recommendation(recommendation: RetentionRecommendation | None) -> str:
    if recommendation is None:
        return "score=n/a confidence=n/a"
    reason_text = "; ".join(recommendation.reasons[:4])
    if len(recommendation.reasons) > 4:
        reason_text += f"; +{len(recommendation.reasons) - 4} more"
    return f"score={recommendation.score} confidence={recommendation.confidence} reasons={reason_text}"


def bank_candidate_groups(match: str = "normalized_name") -> list[DedupeGroup]:
    groups: dict[str, list[Bank]] = defaultdict(list)
    for bank in Bank.objects.all().order_by("name", "created_at", "id"):
        if match == "domain":
            key = clean_text(bank.website_domain).lower()
        elif match == "exact_name":
            key = clean_text(bank.name).lower()
        else:
            key = normalize_bank_name(bank.name)
        if key:
            groups[key].append(bank)
    return [
        DedupeGroup(key=key, items=sorted(items, key=_bank_score), match_type=match)
        for key, items in sorted(groups.items())
        if len(items) > 1
    ]


def contact_candidate_groups(match: str = "email") -> list[DedupeGroup]:
    groups: dict[str, list[Contact]] = defaultdict(list)
    for contact in Contact.objects.select_related("bank").all().order_by("name", "created_at", "id"):
        if match == "name_bank":
            name = normalize_contact_name(contact.name)
            if not name:
                continue
            key = f"{name}|{contact.bank_id or ''}"
        elif match == "name":
            key = normalize_contact_name(contact.name)
        else:
            key = clean_text(contact.email).lower()
        if key:
            groups[key].append(contact)
    return [
        DedupeGroup(key=key, items=sorted(items, key=_contact_score), match_type=match)
        for key, items in sorted(groups.items())
        if len(items) > 1
    ]


def summarize_bank(bank: Bank) -> str:
    return (
        f"{bank.name or '(blank)'} | id={bank.id} | domain={bank.website_domain or '-'} | "
        f"deals={bank.deals.count()} contacts={bank.contacts.count()}"
    )


def summarize_contact(contact: Contact) -> str:
    return (
        f"{contact.name or '(blank)'} | id={contact.id} | email={contact.email or '-'} | "
        f"bank={contact.bank.name if contact.bank else '-'} | "
        f"primary_deals={contact.primary_deals.count()} additional_deals={contact.additional_deals.count()} "
        f"meetings={contact.meetings.count()}"
    )


@transaction.atomic
def merge_bank_into_canonical(canonical: Bank, duplicate: Bank) -> None:
    if canonical.id == duplicate.id:
        return

    changed_fields = []
    if not clean_text(canonical.name) and clean_text(duplicate.name):
        canonical.name = duplicate.name
        changed_fields.append("name")
    if not clean_text(canonical.website_domain) and clean_text(duplicate.website_domain):
        canonical.website_domain = duplicate.website_domain
        changed_fields.append("website_domain")
    merged_description = merge_text_value(canonical.description, duplicate.description)
    if merged_description != canonical.description:
        canonical.description = merged_description
        changed_fields.append("description")
    if changed_fields:
        canonical.save(update_fields=list(dict.fromkeys(changed_fields)))

    Deal.objects.filter(bank=duplicate).update(bank=canonical)
    for contact in Contact.objects.filter(bank=duplicate):
        contact.bank = canonical
        contact.save(update_fields=["bank"])
        sync_primary_contact_bank(contact)

    duplicate.delete()


@transaction.atomic
def merge_contact_into_canonical(canonical: Contact, duplicate: Contact) -> None:
    if canonical.id == duplicate.id:
        return

    changed_fields = []
    fill_fields = [
        "name",
        "email",
        "designation",
        "address",
        "bank",
        "location",
        "phone",
        "rank",
        "linkedin_url",
        "twitter_handle",
        "ranking",
        "primary_coverage_person",
        "secondary_coverage_person",
        "pipeline",
        "last_meeting_date",
    ]
    for field in fill_fields:
        canonical_value = getattr(canonical, field)
        duplicate_value = getattr(duplicate, field)
        empty = canonical_value is None or canonical_value == "" or canonical_value == []
        if empty and duplicate_value not in (None, "", []):
            setattr(canonical, field, duplicate_value)
            changed_fields.append(field)

    for field in ("follow_ups",):
        merged = merge_text_value(getattr(canonical, field), getattr(duplicate, field))
        if merged != getattr(canonical, field):
            setattr(canonical, field, merged)
            changed_fields.append(field)

    for field in ("responsibility", "sector_coverage"):
        merged = merge_list_value(getattr(canonical, field), getattr(duplicate, field))
        if merged != list(getattr(canonical, field) or []):
            setattr(canonical, field, merged)
            changed_fields.append(field)

    summed_source_count = int(canonical.source_count or 0) + int(duplicate.source_count or 0)
    if summed_source_count != int(canonical.source_count or 0):
        canonical.source_count = summed_source_count
        changed_fields.append("source_count")

    summed_total_deals = int(canonical.total_deals_legacy or 0) + int(duplicate.total_deals_legacy or 0)
    if summed_total_deals != int(canonical.total_deals_legacy or 0):
        canonical.total_deals_legacy = summed_total_deals
        changed_fields.append("total_deals_legacy")

    if changed_fields:
        canonical.save(update_fields=list(dict.fromkeys(changed_fields)))

    Deal.objects.filter(primary_contact=duplicate).update(primary_contact=canonical)
    for deal in Deal.objects.filter(additional_contacts=duplicate).distinct():
        deal.additional_contacts.remove(duplicate)
        if deal.primary_contact_id != canonical.id:
            deal.additional_contacts.add(canonical)
        sync_deal_contact_links(deal)

    duplicate_id = str(duplicate.id)
    canonical_id = str(canonical.id)
    for deal in Deal.objects.filter(other_contacts__contains=[duplicate_id]):
        replacement = []
        for value in deal.other_contacts or []:
            text = str(value)
            if text == duplicate_id:
                text = canonical_id
            if text not in replacement and text != str(deal.primary_contact_id or ""):
                replacement.append(text)
        deal.other_contacts = replacement
        deal.save(update_fields=["other_contacts"])

    try:
        from meetings.models import MeetingContact

        for link in MeetingContact.objects.filter(contact=duplicate):
            if MeetingContact.objects.filter(meeting=link.meeting, contact=canonical).exists():
                link.delete()
            else:
                link.contact = canonical
                link.save(update_fields=["contact"])
    except Exception:
        # Meetings is optional for local maintenance scripts; do not block contact merges.
        pass

    duplicate.delete()


def merge_many_banks(canonical: Bank, duplicates: Iterable[Bank]) -> None:
    for duplicate in duplicates:
        merge_bank_into_canonical(canonical, duplicate)


def merge_many_contacts(canonical: Contact, duplicates: Iterable[Contact]) -> None:
    for duplicate in duplicates:
        merge_contact_into_canonical(canonical, duplicate)
