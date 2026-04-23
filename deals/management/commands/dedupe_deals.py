from __future__ import annotations

import json

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count

from ai_orchestrator.models import DealRetrievalProfile, DocumentChunk
from deals.models import Deal, DealAnalysis, DealDocument, DealPhaseLog
from microsoft.models import Email


SCALAR_FIELDS = [
    "bank",
    "priority",
    "deal_status",
    "current_phase",
    "rejection_stage_id",
    "rejection_reason",
    "deal_summary",
    "funding_ask",
    "industry",
    "sector",
    "comments",
    "deal_details",
    "funding_ask_for",
    "company_details",
    "request",
    "reasons_for_passing",
    "city",
    "state",
    "country",
    "primary_contact",
    "fund",
    "legacy_investment_bank",
    "priority_rationale",
    "extracted_text",
    "source_onedrive_id",
    "source_drive_id",
    "source_email_id",
    "processing_status",
    "processing_error",
]


def merge_text(base: str | None, incoming: str | None) -> str | None:
    base = (base or "").strip()
    incoming = (incoming or "").strip()
    if not incoming:
        return base or None
    if not base:
        return incoming
    if incoming in base:
        return base
    if base in incoming:
        return incoming
    return f"{base}\n\n{incoming}"


def merge_list(base, incoming):
    merged = []
    for value in list(base or []) + list(incoming or []):
        if value not in merged:
            merged.append(value)
    return merged


def deal_rank_key(deal: Deal):
    return (
        -deal.documents.count(),
        -deal.chunks.count(),
        -deal.analyses.count(),
        deal.created_at.isoformat() if deal.created_at else "",
        str(deal.id),
    )


def document_identity_key(document: DealDocument):
    onedrive_id = (document.onedrive_id or "").strip()
    if onedrive_id:
        return ("onedrive", onedrive_id.lower())
    title = (document.title or "").strip().lower()
    doc_type = (document.document_type or "").strip().lower()
    return ("title", title, doc_type)


def chunk_identity_key(chunk: DocumentChunk):
    metadata = chunk.metadata or {}
    normalized_metadata = json.dumps(metadata, sort_keys=True, default=str)
    return (
        (chunk.source_type or "").strip().lower(),
        (chunk.source_id or "").strip().lower(),
        (chunk.content or "").strip(),
        normalized_metadata,
    )


def newest_first_key(instance):
    created_at = instance.created_at.isoformat() if getattr(instance, "created_at", None) else ""
    return (created_at, str(instance.id))


class Command(BaseCommand):
    help = "Merge duplicate Deal rows by title without changing the current schema."

    def add_arguments(self, parser):
        parser.add_argument("--title", action="append", help="Optional exact title to dedupe. Can be passed multiple times.")
        parser.add_argument("--dry-run", action="store_true", help="Show what would be merged without writing changes.")

    def handle(self, *args, **options):
        titles = [value.strip() for value in options["title"] or [] if value and value.strip()]
        dry_run = options["dry_run"]

        duplicate_groups = (
            Deal.objects.values("title")
            .annotate(count=Count("id"))
            .filter(count__gt=1)
            .order_by("title")
        )
        if titles:
            duplicate_groups = duplicate_groups.filter(title__in=titles)

        groups = list(duplicate_groups)
        if not groups:
            self.stdout.write(self.style.SUCCESS("No duplicate deal titles found for the requested scope."))
            return

        self.stdout.write(f"Found {len(groups)} duplicate title groups.")
        for group in groups:
            title = group["title"]
            deals = list(Deal.objects.filter(title=title).order_by("created_at", "id"))
            deals.sort(key=deal_rank_key)
            canonical = deals[0]
            duplicates = deals[1:]
            self.stdout.write(f"{title}: canonical={canonical.id} duplicates={len(duplicates)}")
            for duplicate in duplicates:
                self.stdout.write(f"  merge {duplicate.id} -> {canonical.id}")
                if dry_run:
                    continue
                self._merge_duplicate_into_canonical(canonical, duplicate)

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run only. No changes written."))
        else:
            self.stdout.write(self.style.SUCCESS("Deal dedupe complete."))

    @transaction.atomic
    def _merge_duplicate_into_canonical(self, canonical: Deal, duplicate: Deal):
        changed_fields: list[str] = []

        for field in SCALAR_FIELDS:
            canonical_value = getattr(canonical, field)
            duplicate_value = getattr(duplicate, field)
            if field in {"deal_summary", "comments", "deal_details", "company_details", "reasons_for_passing", "extracted_text"}:
                merged = merge_text(canonical_value, duplicate_value)
                if merged != canonical_value:
                    setattr(canonical, field, merged)
                    changed_fields.append(field)
                continue
            if canonical_value in (None, "", []):
                if duplicate_value not in (None, "", []):
                    setattr(canonical, field, duplicate_value)
                    changed_fields.append(field)

        merged_themes = merge_list(canonical.themes, duplicate.themes)
        if merged_themes != list(canonical.themes or []):
            canonical.themes = merged_themes
            changed_fields.append("themes")

        merged_other_contacts = merge_list(canonical.other_contacts, duplicate.other_contacts)
        if merged_other_contacts != list(canonical.other_contacts or []):
            canonical.other_contacts = merged_other_contacts
            changed_fields.append("other_contacts")

        canonical.is_indexed = canonical.is_indexed or duplicate.is_indexed
        canonical.is_female_led = canonical.is_female_led or duplicate.is_female_led
        canonical.management_meeting = canonical.management_meeting or duplicate.management_meeting
        canonical.business_proposal_stage = canonical.business_proposal_stage or duplicate.business_proposal_stage
        canonical.ic_stage = canonical.ic_stage or duplicate.ic_stage
        changed_fields.extend([
            "is_indexed",
            "is_female_led",
            "management_meeting",
            "business_proposal_stage",
            "ic_stage",
        ])

        if changed_fields:
            canonical.save(update_fields=list(dict.fromkeys(changed_fields)))

        self._dedupe_documents(canonical, duplicate)
        self._dedupe_chunks(canonical, duplicate)
        DealPhaseLog.objects.filter(deal=duplicate).update(deal=canonical)
        Email.objects.filter(deal=duplicate).update(deal=canonical)

        existing_profile = DealRetrievalProfile.objects.filter(deal=canonical).first()
        duplicate_profile = DealRetrievalProfile.objects.filter(deal=duplicate).first()
        if duplicate_profile:
            if existing_profile:
                duplicate_profile.delete()
            else:
                duplicate_profile.deal = canonical
                duplicate_profile.save(update_fields=["deal"])

        next_version = canonical.analyses.order_by("-version").values_list("version", flat=True).first() or 0
        for analysis in duplicate.analyses.order_by("version", "created_at"):
            next_version += 1
            analysis.deal = canonical
            analysis.version = next_version
            analysis.save(update_fields=["deal", "version"])

        canonical.additional_contacts.add(*duplicate.additional_contacts.all())
        canonical.responsibility.add(*duplicate.responsibility.all())
        duplicate.additional_contacts.clear()
        duplicate.responsibility.clear()

        duplicate.delete()

    def _dedupe_documents(self, canonical: Deal, duplicate: Deal):
        existing_by_key = {
            document_identity_key(document): document
            for document in canonical.documents.all().order_by("-created_at", "-id")
        }
        duplicate_docs = list(duplicate.documents.all().order_by("-created_at", "-id"))
        for document in duplicate_docs:
            key = document_identity_key(document)
            existing = existing_by_key.get(key)
            if existing is None:
                document.deal = canonical
                document.save(update_fields=["deal"])
                existing_by_key[key] = document
                continue

            winner, loser = sorted([existing, document], key=newest_first_key, reverse=True)
            if loser.id == existing.id:
                loser.delete()
                document.deal = canonical
                document.save(update_fields=["deal"])
                existing_by_key[key] = document
            else:
                document.delete()

    def _dedupe_chunks(self, canonical: Deal, duplicate: Deal):
        existing_by_key = {
            chunk_identity_key(chunk): chunk
            for chunk in canonical.chunks.all().order_by("-created_at", "-id")
        }
        duplicate_chunks = list(duplicate.chunks.all().order_by("-created_at", "-id"))
        for chunk in duplicate_chunks:
            key = chunk_identity_key(chunk)
            existing = existing_by_key.get(key)
            if existing is None:
                chunk.deal = canonical
                chunk.save(update_fields=["deal"])
                existing_by_key[key] = chunk
                continue

            winner, loser = sorted([existing, chunk], key=newest_first_key, reverse=True)
            if loser.id == existing.id:
                loser.delete()
                chunk.deal = canonical
                chunk.save(update_fields=["deal"])
                existing_by_key[key] = chunk
            else:
                chunk.delete()
