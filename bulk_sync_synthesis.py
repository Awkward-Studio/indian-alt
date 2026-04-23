import argparse
import gc
import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path

import django

# Setup Django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.base")
django.setup()

from django.db import connections, transaction
from django.utils import timezone

from ai_orchestrator.services.embedding_processor import EmbeddingService
from banks.models import Bank
from contacts.models import Contact
from deals.models import (
    AnalysisKind,
    ChunkingStatus,
    Deal,
    DealAnalysis,
    DealDocument,
    DocumentType,
    ExtractionMode,
    InitialAnalysisStatus,
    TranscriptionStatus,
)
from deals.services.contact_linking import sync_deal_contact_links
from deals.services.bulk_sync_resolution import (
    normalize_placeholder,
    normalized_deal_name,
    resolve_existing_deal,
)
from deals.services.deal_creation import DealCreationService
from deals.services.document_artifacts import DocumentArtifactService


SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR / "data" / "extractions"


def normalize_string_list(values):
    result = []
    for value in values or []:
        cleaned = normalize_placeholder(value)
        if isinstance(cleaned, str) and cleaned not in result:
            result.append(cleaned)
    return result


def iter_target_dirs(base_dir, target_deals=None):
    targets = set(target_deals or [])
    for deal_dir in sorted(base_dir.iterdir()):
        if not deal_dir.is_dir():
            continue
        if targets and deal_dir.name not in targets:
            continue
        yield deal_dir


def lookup_or_create_deal(deal_dir, artifact_data, dry_run=False):
    resolution = resolve_existing_deal(deal_dir.name, artifact_data)
    if resolution.deal:
        if resolution.duplicates:
            print(
                f"[WARN] {resolution.canonical_title}: found {len(resolution.duplicates) + 1} matching deal rows; "
                f"using canonical deal {resolution.deal.id} matched by {resolution.matched_by!r}",
                flush=True,
            )
        return resolution.deal, False

    if dry_run:
        preview_title = resolution.canonical_title or normalized_deal_name(deal_dir.name, artifact_data) or deal_dir.name
        return Deal(title=preview_title), True

    created_title = resolution.canonical_title or normalized_deal_name(deal_dir.name, artifact_data) or deal_dir.name
    return Deal.objects.get_or_create(title=created_title)


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
    metadata["documents_analyzed"] = normalize_string_list(
        metadata.get("documents_analyzed")
        or [doc.get("document_name") for doc in documents_used if doc.get("document_name")]
    )
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


def normalize_document_type(value):
    cleaned = normalize_placeholder(value)
    if cleaned in {choice for choice, _ in DocumentType.choices}:
        return cleaned
    return DocumentType.OTHER


def normalize_extraction_mode(value):
    cleaned = normalize_placeholder(value)
    if cleaned in {choice for choice, _ in ExtractionMode.choices}:
        return cleaned
    return ExtractionMode.FALLBACK_TEXT


def infer_transcription_status(normalized_text):
    return TranscriptionStatus.COMPLETE if normalize_placeholder(normalized_text) else TranscriptionStatus.FAILED


def artifact_source_id(artifact):
    if not isinstance(artifact, dict):
        return None
    source_map = artifact.get("source_map") if isinstance(artifact.get("source_map"), dict) else {}
    return normalize_placeholder(
        source_map.get("source_id")
        or source_map.get("file_id")
        or source_map.get("onedrive_id")
    )


def iter_document_artifact_paths(deal_dir):
    for artifact_path in sorted(deal_dir.glob("*.artifact.json")):
        if artifact_path.name == "DEAL_SYNTHESIS.artifact.json":
            continue
        yield artifact_path


def artifact_fingerprint(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def build_document_sync_items(deal, deal_dir, synth_artifact):
    documents_used = (
        ((synth_artifact.get("metadata") or {}).get("documents_used") or [])
        if isinstance(synth_artifact, dict)
        else []
    )
    analyzed_names = {
        str(item.get("document_name")).strip().lower()
        for item in documents_used
        if isinstance(item, dict) and item.get("document_name")
    }

    items = []

    for artifact_path in iter_document_artifact_paths(deal_dir):
        with open(artifact_path, "r") as f:
            raw_artifact = json.load(f)

        artifact = DocumentArtifactService.artifact_from_file_record(
            {
                "file_name": raw_artifact.get("document_name") or artifact_path.name.replace(".artifact.json", ""),
                "document_type": raw_artifact.get("document_type") or DocumentType.OTHER,
                "extracted_text": raw_artifact.get("normalized_text") or "",
                "extraction_mode": raw_artifact.get("extraction_mode"),
                "document_artifact": raw_artifact,
            }
        )
        title = normalize_placeholder(artifact.get("document_name")) or artifact_path.name.replace(".artifact.json", "")
        normalized_text = (artifact.get("normalized_text") or "").strip()
        source_id = artifact_source_id(artifact)
        raw_fingerprint = artifact_fingerprint(raw_artifact)

        lookup = {"deal": deal, "title": title}
        existing_doc = None
        if source_id:
            existing_doc = DealDocument.objects.filter(deal=deal, onedrive_id=source_id).first()
            if existing_doc:
                lookup = {"id": existing_doc.id}
        if not existing_doc:
            existing_doc = DealDocument.objects.filter(**lookup).first()

        defaults = {
            "document_type": normalize_document_type(artifact.get("document_type")),
            "onedrive_id": source_id,
            "extracted_text": normalized_text,
            "normalized_text": normalized_text,
            "evidence_json": artifact,
            "source_map_json": artifact.get("source_map") or {},
            "table_json": artifact.get("tables_summary") or [],
            "key_metrics_json": artifact.get("metrics") or [],
            "reasoning": artifact.get("reasoning") or "",
            "is_indexed": False,
            "is_ai_analyzed": title.strip().lower() in analyzed_names,
            "initial_analysis_status": (
                InitialAnalysisStatus.SELECTED_AND_ANALYZED
                if title.strip().lower() in analyzed_names
                else InitialAnalysisStatus.NOT_SELECTED
            ),
            "extraction_mode": normalize_extraction_mode(artifact.get("extraction_mode")),
            "transcription_status": infer_transcription_status(normalized_text),
            "chunking_status": ChunkingStatus.NOT_CHUNKED,
            "last_transcribed_at": timezone.now() if normalized_text else None,
        }
        artifact_with_fingerprint = dict(artifact)
        artifact_with_fingerprint["_sync_artifact_fingerprint"] = raw_fingerprint
        defaults["evidence_json"] = artifact_with_fingerprint

        existing_fingerprint = None
        if existing_doc and isinstance(existing_doc.evidence_json, dict):
            existing_fingerprint = existing_doc.evidence_json.get("_sync_artifact_fingerprint")
            if not existing_fingerprint and existing_doc.evidence_json == artifact:
                existing_fingerprint = raw_fingerprint

        item = {
            "artifact_path": artifact_path,
            "raw_artifact": raw_artifact,
            "artifact": artifact,
            "title": title,
            "normalized_text": normalized_text,
            "source_id": source_id,
            "lookup": lookup,
            "defaults": defaults,
            "existing_doc": existing_doc,
            "artifact_fingerprint": raw_fingerprint,
            "unchanged": bool(existing_doc and existing_fingerprint == raw_fingerprint),
        }
        items.append(item)

    return items


def sync_deal_documents(deal, deal_dir, synth_artifact, dry_run=False):
    items = build_document_sync_items(deal, deal_dir, synth_artifact)
    if dry_run:
        changed_count = sum(1 for item in items if not item["unchanged"])
        return changed_count > 0, f"Would sync {len(items)} document artifacts ({changed_count} changed)"

    embed_service = EmbeddingService()
    synced_docs = []
    indexed_docs = 0
    changed_docs = 0
    skipped_docs = 0

    for item in items:
        if item["unchanged"]:
            skipped_docs += 1
            if item["existing_doc"]:
                synced_docs.append(item["existing_doc"])
            continue

        changed_docs += 1
        doc, _ = DealDocument.objects.update_or_create(defaults=item["defaults"], **item["lookup"])
        synced_docs.append(doc)

        normalized_text = item["normalized_text"]
        if normalized_text and embed_service.vectorize_document(doc):
            indexed_docs += 1
            doc.refresh_from_db(fields=["is_indexed", "chunking_status", "last_chunked_at"])
        elif not normalized_text:
            doc.is_indexed = False
            doc.chunking_status = ChunkingStatus.FAILED
            doc.save(update_fields=["is_indexed", "chunking_status"])

    if changed_docs:
        all_docs = list(deal.documents.order_by("created_at", "id"))
        extracted_segments = []
        for doc in all_docs:
            text = (doc.normalized_text or doc.extracted_text or "").strip()
            if not text:
                continue
            extracted_segments.append(f"--- DOCUMENT: {doc.title} ---\n{text}")
        deal.extracted_text = "\n\n".join(extracted_segments) if extracted_segments else ""
        deal.save(update_fields=["extracted_text"])

    return changed_docs > 0, (
        f"Synced {len(items)} documents, changed {changed_docs}, skipped {skipped_docs}, indexed {indexed_docs}"
    )


def payload_fingerprint(payload, thinking):
    fingerprint_payload = {
        "analysis_json": payload,
        "thinking": thinking or "",
    }
    return hashlib.sha256(json.dumps(fingerprint_payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def current_synthesis_fingerprint(deal, synth_artifact, investment_report_text=None, investment_report_path=None):
    latest_analysis = deal.analyses.order_by("-version", "-created_at").first() if getattr(deal, "pk", None) else None
    if not latest_analysis:
        return None, None, None, None
    analysis_payload = build_analysis_payload(
        synth_artifact,
        investment_report_text=investment_report_text,
        investment_report_path=investment_report_path,
    )
    previous_snapshot = ((deal.current_analysis or {}).get("canonical_snapshot") or {}) if getattr(deal, "pk", None) else {}
    normalized_analysis = DealCreationService.normalize_analysis_payload(
        analysis_payload,
        previous_snapshot=previous_snapshot,
        analysis_kind=AnalysisKind.SUPPLEMENTAL,
        documents_analyzed=(analysis_payload.get("metadata") or {}).get("documents_analyzed"),
        analysis_input_files=(analysis_payload.get("metadata") or {}).get("analysis_input_files"),
        failed_files=(analysis_payload.get("metadata") or {}).get("failed_files"),
    )
    incoming_fingerprint = payload_fingerprint(normalized_analysis, synth_artifact.get("thinking_process") or "")
    existing_fingerprint = payload_fingerprint(latest_analysis.analysis_json or {}, latest_analysis.thinking or "")
    return latest_analysis, normalized_analysis, incoming_fingerprint, existing_fingerprint


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
        bank = Bank.objects.create(name=name, website_domain=domain, description=description)
        return bank

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
def sync_synthesis_artifact(deal, synth_artifact, investment_report_text=None, investment_report_path=None, dry_run=False):
    analysis_payload = build_analysis_payload(
        synth_artifact,
        investment_report_text=investment_report_text,
        investment_report_path=investment_report_path,
    )
    latest_analysis = deal.analyses.order_by("-version", "-created_at").first() if getattr(deal, "pk", None) else None
    previous_snapshot = ((deal.current_analysis or {}).get("canonical_snapshot") or {}) if getattr(deal, "pk", None) else {}
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
            return "DRY-RUN", "Would skip unchanged synthesis", False
        return "DRY-RUN", f"Would import synthesis as v{next_version}", True

    DealCreationService.apply_analysis_to_deal(
        deal,
        normalized_analysis,
        overwrite=True,
        overwrite_themes=True,
    )
    _, rename_message = apply_extended_deal_fields(
        deal,
        normalized_analysis.get("deal_model_data"),
        overwrite=True,
        dry_run=False,
    )
    if rename_message:
        print(rename_message, flush=True)
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


def parse_args():
    parser = argparse.ArgumentParser(description="Import DEAL_SYNTHESIS.artifact.json files into DealAnalysis and related models.")
    parser.add_argument("--deals", nargs="*", help="Optional extraction folder names to import. Defaults to all.")
    parser.add_argument("--dry-run", action="store_true", help="Preview what would be imported without writing to the database.")
    parser.add_argument(
        "--skip-existing",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Skip deals whose synthesis and document artifacts are already present and unchanged.",
    )
    return parser.parse_args()


def prompt_skip_existing(default=True):
    prompt = "Skip deals that are already present and unchanged? [Y/n]: " if default else "Skip deals that are already present and unchanged? [y/N]: "
    try:
        response = input(prompt).strip().lower()
    except EOFError:
        return default
    if not response:
        return default
    return response in {"y", "yes"}


def run():
    args = parse_args()

    if not BASE_DIR.exists():
        print(f"Error: {BASE_DIR} not found.")
        return

    print("\n>>> SYNTHESIS SYNC: LOADING PHASE 3 DEAL SYNTHESIS")
    print("-" * 72)

    if args.skip_existing is None:
        skip_existing = True if args.dry_run else prompt_skip_existing(default=True)
    else:
        skip_existing = args.skip_existing
    print(f"Skip unchanged existing deals: {'yes' if skip_existing else 'no'}")

    processed = 0
    skipped = 0
    errors = 0

    for deal_dir in iter_target_dirs(BASE_DIR, target_deals=args.deals):
        synthesis_path = deal_dir / "DEAL_SYNTHESIS.artifact.json"
        investment_report_path = deal_dir / "INVESTMENT_REPORT.md"
        if not synthesis_path.exists():
            continue

        try:
            with open(synthesis_path, "r") as f:
                synth_artifact = json.load(f)
            investment_report_text = None
            if investment_report_path.exists():
                investment_report_text = investment_report_path.read_text(encoding="utf-8").strip()

            deal_obj, created = lookup_or_create_deal(deal_dir, synth_artifact, dry_run=args.dry_run)
            latest_analysis, _, incoming_fingerprint, existing_fingerprint = current_synthesis_fingerprint(
                deal_obj,
                synth_artifact,
                investment_report_text=investment_report_text,
                investment_report_path=str(investment_report_path.name) if investment_report_text else None,
            )
            doc_items = build_document_sync_items(deal_obj, deal_dir, synth_artifact)
            docs_changed = any(not item["unchanged"] for item in doc_items)
            synthesis_changed = not latest_analysis or incoming_fingerprint != existing_fingerprint

            if skip_existing and not created and not synthesis_changed and not docs_changed and not args.dry_run:
                skipped += 1
                print(f"[SKIP] {deal_obj.title}: synthesis and document artifacts unchanged")
                continue

            status, message, synthesis_applied = sync_synthesis_artifact(
                deal_obj,
                synth_artifact,
                investment_report_text=investment_report_text,
                investment_report_path=str(investment_report_path.name) if investment_report_text else None,
                dry_run=args.dry_run,
            )
            docs_applied, doc_message = sync_deal_documents(
                deal_obj,
                deal_dir,
                synth_artifact,
                dry_run=args.dry_run,
            )
            if docs_applied or synthesis_applied:
                embed_service = EmbeddingService()
                if synthesis_applied:
                    embed_service.vectorize_deal(deal_obj)
                embed_service.refresh_deal_profile(deal_obj)
            if status == "SKIP":
                skipped += 1
            else:
                processed += 1

            creation_note = " [new deal]" if created else ""
            print(f"[{status}] {deal_obj.title}{creation_note}: {message}")
            print(f"  [DOCS] {doc_message}")
        except Exception as exc:
            errors += 1
            print(f"[ERROR] {deal_dir.name}: {exc}")
        finally:
            for conn in connections.all():
                conn.close()
            gc.collect()

    print("-" * 72)
    print(f"Complete. Processed={processed} Skipped={skipped} Errors={errors}")


if __name__ == "__main__":
    run()
