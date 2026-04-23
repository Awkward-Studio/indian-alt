from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from django.db import transaction
from django.utils import timezone

from ai_orchestrator.services.embedding_processor import EmbeddingService
from banks.models import Bank
from contacts.models import Contact
from deals.models import AnalysisKind, Deal, DealAnalysis
from deals.services.bulk_sync_resolution import normalize_placeholder
from deals.services.contact_linking import sync_deal_contact_links
from deals.services.deal_creation import DealCreationService


def build_analysis_input_files(documents_used):
    files = []
    for doc in documents_used or []:
        file_name = normalize_placeholder(doc.get("document_name")) or normalize_placeholder(doc.get("source_file"))
        if not file_name:
            continue
        file_info = {"file_name": file_name}
        source_file = normalize_placeholder(doc.get("source_file"))
        if source_file:
            file_info["source_file"] = source_file
        doc_type = normalize_placeholder(doc.get("document_type"))
        if doc_type:
            file_info["document_type"] = doc_type
        files.append(file_info)
    return files


def build_analysis_payload(synth_artifact, investment_report_text=None, investment_report_path=None):
    portable_data = deepcopy(synth_artifact.get("portable_deal_data") or {})
    artifact_meta = synth_artifact.get("metadata") if isinstance(synth_artifact.get("metadata"), dict) else {}
    synthesis_metadata = portable_data.get("metadata") if isinstance(portable_data.get("metadata"), dict) else {}
    documents_used = artifact_meta.get("documents_used") if isinstance(artifact_meta.get("documents_used"), list) else []

    portable_data["document_evidence"] = documents_used
    portable_data["thinking"] = synth_artifact.get("thinking_process")

    metadata = dict(synthesis_metadata)
    metadata["documents_analyzed"] = [
        value for value in (
            metadata.get("documents_analyzed")
            or [doc.get("document_name") for doc in documents_used if doc.get("document_name")]
        )
        if value
    ]
    metadata["analysis_input_files"] = build_analysis_input_files(documents_used)
    metadata["failed_files"] = list(metadata.get("failed_files") or [])
    metadata["source_artifact_version"] = artifact_meta.get("version")
    metadata["source_artifact_timestamp"] = artifact_meta.get("timestamp")
    metadata["documents_used_count"] = artifact_meta.get("documents_used_count", len(documents_used))
    if investment_report_path:
        metadata["source_investment_report_file"] = investment_report_path
    portable_data["metadata"] = metadata
    if normalize_placeholder(investment_report_text):
        portable_data["analyst_report"] = investment_report_text.strip()
    return portable_data


def payload_fingerprint(payload, thinking):
    fingerprint_payload = {
        "analysis_json": payload,
        "thinking": thinking or "",
    }
    return hashlib.sha256(json.dumps(fingerprint_payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def apply_extended_deal_fields(deal, model_data, overwrite=True, dry_run=False):
    if not isinstance(model_data, dict):
        return [], None

    changed_fields = []
    rename_message = None
    string_mappings = {
        "deal_details": "deal_details",
        "company_details": "company_details",
        "priority_rationale": "priority_rationale",
    }
    for source_key, deal_field in string_mappings.items():
        value = normalize_placeholder(model_data.get(source_key))
        if not isinstance(value, str):
            continue
        current = getattr(deal, deal_field)
        if overwrite or not current:
            if current != value:
                setattr(deal, deal_field, value)
                changed_fields.append(deal_field)

    if "is_female_led" in model_data:
        bool_value = bool(model_data.get("is_female_led"))
        if overwrite or deal.is_female_led is None:
            if deal.is_female_led != bool_value:
                deal.is_female_led = bool_value
                changed_fields.append("is_female_led")

    if changed_fields and not dry_run:
        deal.save(update_fields=list(dict.fromkeys(changed_fields)))
    return changed_fields, rename_message


def resolve_bank(bank_payload, dry_run=False):
    if not isinstance(bank_payload, dict):
        return None

    name = normalize_placeholder(bank_payload.get("name"))
    domain = normalize_placeholder(bank_payload.get("website_domain"))
    description = normalize_placeholder(bank_payload.get("description"))

    if not name and not domain:
        return None

    bank = None
    if domain:
        bank = Bank.objects.filter(website_domain__iexact=domain).first()
    if not bank and name:
        bank = Bank.objects.filter(name__iexact=name).first()
    if not bank and name:
        bank = Bank.objects.filter(name__icontains=name).first()

    if not bank:
        if dry_run:
            return Bank(name=name, website_domain=domain, description=description)
        return Bank.objects.create(name=name, website_domain=domain, description=description)

    updated_fields = []
    if name and bank.name != name:
        bank.name = name
        updated_fields.append("name")
    if domain and bank.website_domain != domain:
        bank.website_domain = domain
        updated_fields.append("website_domain")
    if description and bank.description != description:
        bank.description = description
        updated_fields.append("description")
    if updated_fields and not dry_run:
        bank.save(update_fields=updated_fields)
    return bank


def resolve_contact(contact_payload, bank=None, dry_run=False):
    if not isinstance(contact_payload, dict):
        return None

    name = normalize_placeholder(contact_payload.get("name"))
    email = normalize_placeholder(contact_payload.get("email"))
    designation = normalize_placeholder(contact_payload.get("designation"))
    linkedin_url = normalize_placeholder(contact_payload.get("linkedin_url"))
    phone = normalize_placeholder(contact_payload.get("phone"))
    location = normalize_placeholder(contact_payload.get("location"))

    if not name and not email:
        return None

    contact = None
    if email:
        contact = Contact.objects.filter(email__iexact=email).first()
    if not contact and name and bank and getattr(bank, "pk", None):
        contact = Contact.objects.filter(name__iexact=name, bank=bank).first()
    if not contact and name:
        contact = Contact.objects.filter(name__iexact=name).first()

    if not contact:
        if dry_run:
            return Contact(
                name=name,
                email=email,
                designation=designation,
                linkedin_url=linkedin_url,
                phone=phone,
                location=location,
                bank=bank if getattr(bank, "pk", None) else None,
            )
        return Contact.objects.create(
            name=name,
            email=email,
            designation=designation,
            linkedin_url=linkedin_url,
            phone=phone,
            location=location,
            bank=bank if getattr(bank, "pk", None) else None,
        )

    updated_fields = []
    for field, value in (
        ("name", name),
        ("email", email),
        ("designation", designation),
        ("linkedin_url", linkedin_url),
        ("phone", phone),
        ("location", location),
    ):
        if value and getattr(contact, field) != value:
            setattr(contact, field, value)
            updated_fields.append(field)

    if bank and getattr(bank, "pk", None) and contact.bank_id != bank.id:
        contact.bank = bank
        updated_fields.append("bank")

    if updated_fields and not dry_run:
        contact.save(update_fields=list(dict.fromkeys(updated_fields)))
    return contact


def import_relationships(deal, analysis_payload, dry_run=False):
    relationships = analysis_payload.get("source_relationships") if isinstance(analysis_payload, dict) else {}
    if not isinstance(relationships, dict):
        return {"bank": None, "primary_contact": None, "additional_contacts": []}

    bank = resolve_bank(relationships.get("bank"), dry_run=dry_run)
    primary_contact = resolve_contact(relationships.get("primary_contact"), bank=bank, dry_run=dry_run)

    additional_contacts = []
    for item in relationships.get("additional_contacts") or []:
        contact = resolve_contact(item, bank=bank, dry_run=dry_run)
        if contact and all(str(existing.id) != str(contact.id) for existing in additional_contacts if getattr(existing, "id", None)):
            additional_contacts.append(contact)
        elif contact and not getattr(contact, "id", None):
            additional_contacts.append(contact)

    if dry_run:
        return {"bank": bank, "primary_contact": primary_contact, "additional_contacts": additional_contacts}

    update_fields = []
    if bank and getattr(bank, "pk", None):
        if deal.bank_id != bank.id:
            deal.bank = bank
            update_fields.append("bank")
        if normalize_placeholder(bank.name) and deal.legacy_investment_bank != bank.name:
            deal.legacy_investment_bank = bank.name
            update_fields.append("legacy_investment_bank")
    if update_fields:
        deal.save(update_fields=list(dict.fromkeys(update_fields)))

    sync_deal_contact_links(
        deal,
        primary_contact=primary_contact if getattr(primary_contact, "pk", None) else None,
        primary_contact_provided=primary_contact is not None,
        additional_contacts=[c for c in additional_contacts if getattr(c, "pk", None)],
        additional_contacts_provided=True,
    )

    return {"bank": bank, "primary_contact": primary_contact, "additional_contacts": additional_contacts}


@transaction.atomic
def sync_synthesis_artifact(
    deal,
    synth_artifact,
    investment_report_text=None,
    investment_report_path=None,
    dry_run=False,
    preserve_history=False,
):
    analysis_payload = build_analysis_payload(
        synth_artifact,
        investment_report_text=investment_report_text,
        investment_report_path=investment_report_path,
    )
    latest_analysis = deal.analyses.order_by("-version", "-created_at").first() if getattr(deal, "pk", None) else None
    previous_snapshot = ((deal.current_analysis or {}).get("canonical_snapshot") or {}) if getattr(deal, "pk", None) else {}
    if not preserve_history:
        latest_analysis = None
        previous_snapshot = {}

    analysis_kind = AnalysisKind.INITIAL if not latest_analysis else AnalysisKind.SUPPLEMENTAL
    next_version = 1 if not latest_analysis else latest_analysis.version + 1

    normalized_analysis = DealCreationService.normalize_analysis_payload(
        analysis_payload,
        previous_snapshot=previous_snapshot,
        analysis_kind=analysis_kind,
        documents_analyzed=(analysis_payload.get("metadata") or {}).get("documents_analyzed"),
        analysis_input_files=(analysis_payload.get("metadata") or {}).get("analysis_input_files"),
        failed_files=(analysis_payload.get("metadata") or {}).get("failed_files"),
    )
    thinking = synth_artifact.get("thinking_process") or ""

    incoming_fingerprint = payload_fingerprint(normalized_analysis, thinking)
    existing_fingerprint = None
    if latest_analysis:
        existing_fingerprint = payload_fingerprint(latest_analysis.analysis_json or {}, latest_analysis.thinking or "")

    if dry_run:
        if latest_analysis and incoming_fingerprint == existing_fingerprint:
            return "DRY-RUN", "Would refresh deal from unchanged synthesis", False
        return "DRY-RUN", f"Would import synthesis as v{next_version}", True

    DealCreationService.apply_analysis_to_deal(
        deal,
        normalized_analysis,
        overwrite=True,
        overwrite_themes=True,
    )
    apply_extended_deal_fields(
        deal,
        normalized_analysis.get("deal_model_data"),
        overwrite=True,
        dry_run=False,
    )
    import_relationships(deal, normalized_analysis, dry_run=False)

    if latest_analysis and incoming_fingerprint == existing_fingerprint:
        return "OK", "Synthesis unchanged", False

    DealAnalysis.objects.create(
        deal=deal,
        version=next_version,
        analysis_kind=analysis_kind,
        thinking=thinking,
        ambiguities=((normalized_analysis.get("metadata") or {}).get("ambiguous_points") or []),
        analysis_json=normalized_analysis,
    )
    return "OK", f"Imported synthesis as v{next_version}", True


def load_investment_report_text(deal_dir: Path) -> tuple[str | None, str | None]:
    investment_report_path = deal_dir / "INVESTMENT_REPORT.md"
    if not investment_report_path.exists():
        return None, None
    return investment_report_path.read_text(encoding="utf-8").strip(), str(investment_report_path.name)


def refresh_deal_embeddings(deal: Deal, embed_service: EmbeddingService | None = None):
    embedder = embed_service or EmbeddingService()
    summary_embedded = embedder.vectorize_deal(deal)
    profile_refreshed = embedder.refresh_deal_profile(deal)
    if profile_refreshed and not deal.is_indexed:
        deal.is_indexed = True
        deal.save(update_fields=["is_indexed"])
    return summary_embedded, profile_refreshed
