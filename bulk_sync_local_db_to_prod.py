import argparse
import json
import os
import subprocess
from copy import deepcopy
from typing import Iterable

import dj_database_url
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.base")
django.setup()

from django.conf import settings
from django.db import connections, transaction
from django.db.models import Count, Q

from ai_orchestrator.models import AIAuditLog, DealRetrievalProfile, DocumentChunk
from banks.models import Bank
from contacts.models import Contact
from deals.models import Deal, DealAnalysis, DealDocument, DealPhaseLog, FolderAnalysisDocument


SOURCE_DB = "default"
TARGET_DB = "production"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Push fully processed local DB deal data into Railway/Postgres without rerunning embeddings."
    )
    parser.add_argument(
        "--deals",
        nargs="*",
        help="Optional deal titles or UUIDs to sync. Defaults to all deals.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be synced without writing to the production DB.",
    )
    parser.add_argument(
        "--prod-database-url",
        default=os.environ.get("PROD_DATABASE_URL"),
        help="Target production DATABASE_URL. Defaults to PROD_DATABASE_URL only.",
    )
    parser.add_argument(
        "--railway-cli",
        action="store_true",
        help="Read DATABASE_URL from `railway variables --json` if --prod-database-url/PROD_DATABASE_URL is not set.",
    )
    parser.add_argument(
        "--railway-project-dir",
        default=".",
        help="Directory where Railway CLI should run. Defaults to current directory.",
    )
    parser.add_argument(
        "--prune-production-deals",
        action="store_true",
        help="Delete production deals that are not present in the selected local deal set. Dry-run unless --dry-run is omitted.",
    )
    parser.add_argument(
        "--only-missing-deals",
        action="store_true",
        help="Sync only local deals that do not already exist in production (matched by id, then title).",
    )
    parser.add_argument(
        "--prod-db-ssl-require",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Whether the production DB connection should require SSL.",
    )
    return parser.parse_args()


def database_url_from_railway_cli(project_dir: str = ".") -> str | None:
    command = ["railway", "variables", "--json"]
    result = subprocess.run(
        command,
        cwd=project_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout or "{}")
    preferred_keys = (
        "DATABASE_PUBLIC_URL",
        "DATABASE_URL_PUBLIC",
        "POSTGRES_PUBLIC_URL",
        "PGDATABASE_PUBLIC_URL",
        "DATABASE_URL",
    )

    if isinstance(payload, dict):
        for key in preferred_keys:
            value = payload.get(key)
            if value:
                return value
        for item in payload.values():
            if not isinstance(item, dict):
                continue
            if item.get("name") in preferred_keys:
                return item.get("value")
            for key in preferred_keys:
                if item.get(key):
                    return item.get(key)

    if isinstance(payload, list):
        for key in preferred_keys:
            for item in payload:
                if isinstance(item, dict) and item.get("name") == key:
                    return item.get("value")

    return None


def configure_target_database(database_url: str, ssl_require: bool = True):
    if not database_url:
        raise RuntimeError("Missing production database URL. Set PROD_DATABASE_URL or pass --prod-database-url.")
    if ".railway.internal" in database_url:
        raise RuntimeError(
            "Railway returned an internal database URL (*.railway.internal), which cannot be resolved from this "
            "machine. Use Railway's public TCP proxy/database URL instead: set PROD_DATABASE_URL to the public "
            "Postgres URL, pass --prod-database-url, or expose/copy DATABASE_PUBLIC_URL from Railway."
        )

    source_url = settings.DATABASES[SOURCE_DB]
    parsed = dj_database_url.parse(database_url, conn_max_age=600, ssl_require=ssl_require)
    target_config = deepcopy(source_url)
    target_config.update(parsed)

    if source_url.get("ENGINE") == target_config.get("ENGINE"):
        source_name = str(source_url.get("NAME") or "")
        target_name = str(target_config.get("NAME") or "")
        source_host = str(source_url.get("HOST") or "")
        target_host = str(target_config.get("HOST") or "")
        if source_name == target_name and source_host == target_host:
            raise RuntimeError("Source and production databases resolve to the same target. Refusing to run.")

    settings.DATABASES[TARGET_DB] = target_config
    connections.databases[TARGET_DB] = target_config
    connections[TARGET_DB].close()


def fetch_migration_set(alias: str) -> set[tuple[str, str]]:
    with connections[alias].cursor() as cursor:
        cursor.execute("SELECT app, name FROM django_migrations")
        return {(row[0], row[1]) for row in cursor.fetchall()}


def ensure_pgvector(alias: str):
    with connections[alias].cursor() as cursor:
        cursor.execute("SELECT current_database(), current_user")
        db_name, db_user = cursor.fetchone()
        cursor.execute("SELECT COUNT(*) FROM pg_extension WHERE extname = 'vector'")
        if cursor.fetchone()[0] == 0:
            raise RuntimeError(
                f"Target database {db_name} (user {db_user}) does not have pgvector enabled."
            )
        cursor.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        version = cursor.fetchone()[0]
    return {"database": db_name, "user": db_user, "vector_version": version}


def compare_schema_state():
    source_migrations = fetch_migration_set(SOURCE_DB)
    target_migrations = fetch_migration_set(TARGET_DB)
    missing = sorted(source_migrations - target_migrations)
    if missing:
        preview = ", ".join(f"{app}.{name}" for app, name in missing[:10])
        raise RuntimeError(
            "Production database is missing local migrations. "
            f"Run migrations on Railway first. Missing examples: {preview}"
        )


def normalize_text(value):
    if value is None:
        return ""
    return str(value).strip()


def iter_target_deals(identifiers: Iterable[str] | None):
    queryset = Deal.objects.using(SOURCE_DB).all().order_by("title", "created_at")
    identifiers = [normalize_text(value) for value in identifiers or [] if normalize_text(value)]
    if not identifiers:
        return list(queryset)

    matched = []
    seen_ids = set()
    for identifier in identifiers:
        candidates = queryset.filter(Q(id=identifier) | Q(title__iexact=identifier))
        for deal in candidates:
            if str(deal.id) in seen_ids:
                continue
            seen_ids.add(str(deal.id))
            matched.append(deal)
    return matched


def filter_missing_deals(local_deals: list[Deal]) -> tuple[list[Deal], int]:
    filtered: list[Deal] = []
    skipped_existing = 0
    for local_deal in local_deals:
        exists = Deal.objects.using(TARGET_DB).filter(id=local_deal.id).exists()
        if not exists and normalize_text(local_deal.title):
            exists = Deal.objects.using(TARGET_DB).filter(title__iexact=local_deal.title).exists()
        if exists:
            skipped_existing += 1
            continue
        filtered.append(local_deal)
    return filtered, skipped_existing


def upsert_bank(local_bank: Bank | None, dry_run: bool = False) -> Bank | None:
    if not local_bank:
        return None

    prod_bank = Bank.objects.using(TARGET_DB).filter(id=local_bank.id).first()
    if not prod_bank and normalize_text(local_bank.website_domain):
        prod_bank = Bank.objects.using(TARGET_DB).filter(
            website_domain__iexact=local_bank.website_domain
        ).first()
    if not prod_bank and normalize_text(local_bank.name):
        prod_bank = Bank.objects.using(TARGET_DB).filter(name__iexact=local_bank.name).first()

    payload = {
        "name": local_bank.name,
        "website_domain": local_bank.website_domain,
        "description": local_bank.description,
    }

    if dry_run:
        return prod_bank or local_bank

    if prod_bank:
        changed = []
        for field, value in payload.items():
            if getattr(prod_bank, field) != value:
                setattr(prod_bank, field, value)
                changed.append(field)
        if changed:
            prod_bank.save(using=TARGET_DB, update_fields=changed)
        return prod_bank

    return Bank.objects.using(TARGET_DB).create(id=local_bank.id, **payload)


def upsert_contact(local_contact: Contact | None, bank_map: dict[str, Bank], dry_run: bool = False) -> Contact | None:
    if not local_contact:
        return None

    prod_contact = Contact.objects.using(TARGET_DB).filter(id=local_contact.id).first()
    if not prod_contact and normalize_text(local_contact.email):
        prod_contact = Contact.objects.using(TARGET_DB).filter(email__iexact=local_contact.email).first()
    if not prod_contact and normalize_text(local_contact.name):
        query = Contact.objects.using(TARGET_DB).filter(name__iexact=local_contact.name)
        mapped_bank = bank_map.get(str(local_contact.bank_id)) if local_contact.bank_id else None
        if mapped_bank:
            query = query.filter(bank=mapped_bank)
        prod_contact = query.first()

    payload = {
        "name": local_contact.name,
        "email": local_contact.email,
        "designation": local_contact.designation,
        "address": local_contact.address,
        "bank": bank_map.get(str(local_contact.bank_id)) if local_contact.bank_id else None,
        "location": local_contact.location,
        "responsibility": list(local_contact.responsibility or []),
        "phone": local_contact.phone,
        "sector_coverage": list(local_contact.sector_coverage or []),
        "rank": local_contact.rank,
        "linkedin_url": local_contact.linkedin_url,
        "twitter_handle": local_contact.twitter_handle,
        "source_count": local_contact.source_count,
        "ranking": local_contact.ranking,
        "primary_coverage_person": local_contact.primary_coverage_person,
        "secondary_coverage_person": local_contact.secondary_coverage_person,
        "total_deals_legacy": local_contact.total_deals_legacy,
        "pipeline": local_contact.pipeline,
        "follow_ups": local_contact.follow_ups,
        "last_meeting_date": local_contact.last_meeting_date,
    }

    if dry_run:
        return prod_contact or local_contact

    if prod_contact:
        changed = []
        for field, value in payload.items():
            current = getattr(prod_contact, field)
            if current != value:
                setattr(prod_contact, field, value)
                changed.append(field)
        if changed:
            prod_contact.save(using=TARGET_DB, update_fields=changed)
        return prod_contact

    return Contact.objects.using(TARGET_DB).create(id=local_contact.id, **payload)


from django.contrib.auth.models import User
from accounts.models import Profile

def upsert_user_and_profile(local_profile: Profile, dry_run: bool = False) -> Profile:
    local_user = local_profile.user
    prod_user = User.objects.using(TARGET_DB).filter(username=local_user.username).first()
    
    user_payload = {
        "first_name": local_user.first_name,
        "last_name": local_user.last_name,
        "email": local_user.email,
        "is_active": local_user.is_active,
        "is_staff": local_user.is_staff,
        "is_superuser": local_user.is_superuser,
    }

    if not dry_run:
        if prod_user:
            changed = []
            for field, value in user_payload.items():
                if getattr(prod_user, field) != value:
                    setattr(prod_user, field, value)
                    changed.append(field)
            if changed:
                prod_user.save(using=TARGET_DB, update_fields=changed)
        else:
            prod_user = User.objects.using(TARGET_DB).create(
                username=local_user.username,
                password=local_user.password, # Note: this syncs the hashed password
                **user_payload
            )

    prod_profile = Profile.objects.using(TARGET_DB).filter(id=local_profile.id).first()
    if not prod_profile:
        prod_profile = Profile.objects.using(TARGET_DB).filter(email__iexact=local_profile.email).first()

    profile_payload = {
        "user": prod_user if not dry_run else None,
        "name": local_profile.name,
        "email": local_profile.email,
        "image_url": local_profile.image_url,
        "is_admin": local_profile.is_admin,
        "initials": local_profile.initials,
        "is_disabled": local_profile.is_disabled,
    }

    if dry_run:
        return prod_profile or local_profile

    if prod_profile:
        changed = []
        for field, value in profile_payload.items():
            if getattr(prod_profile, field) != value:
                setattr(prod_profile, field, value)
                changed.append(field)
        if changed:
            prod_profile.save(using=TARGET_DB, update_fields=changed)
    else:
        prod_profile = Profile.objects.using(TARGET_DB).create(id=local_profile.id, **profile_payload)
    
    return prod_profile

def upsert_deal(local_deal: Deal, bank_map: dict[str, Bank], contact_map: dict[str, Contact], dry_run: bool = False) -> Deal:
    prod_deal = Deal.objects.using(TARGET_DB).filter(id=local_deal.id).first()
    if not prod_deal and normalize_text(local_deal.title):
        prod_deal = Deal.objects.using(TARGET_DB).filter(title__iexact=local_deal.title).first()

    payload = {
        "title": local_deal.title,
        "bank": bank_map.get(str(local_deal.bank_id)) if local_deal.bank_id else None,
        "priority": local_deal.priority,
        "deal_status": local_deal.deal_status,
        "current_phase": local_deal.current_phase,
        "deal_flow_decisions": dict(local_deal.deal_flow_decisions or {}),
        "rejection_stage_id": local_deal.rejection_stage_id,
        "rejection_reason": local_deal.rejection_reason,
        "deal_summary": local_deal.deal_summary,
        "funding_ask": local_deal.funding_ask,
        "industry": local_deal.industry,
        "sector": local_deal.sector,
        "comments": local_deal.comments,
        "deal_details": local_deal.deal_details,
        "is_female_led": local_deal.is_female_led,
        "management_meeting": local_deal.management_meeting,
        "funding_ask_for": local_deal.funding_ask_for,
        "company_details": local_deal.company_details,
        "business_proposal_stage": local_deal.business_proposal_stage,
        "ic_stage": local_deal.ic_stage,
        "reasons_for_passing": local_deal.reasons_for_passing,
        "city": local_deal.city,
        "state": local_deal.state,
        "country": local_deal.country,
        "other_contacts": [],
        "primary_contact": contact_map.get(str(local_deal.primary_contact_id)) if local_deal.primary_contact_id else None,
        "fund": local_deal.fund,
        "legacy_investment_bank": local_deal.legacy_investment_bank,
        "priority_rationale": local_deal.priority_rationale,
        "themes": list(local_deal.themes or []),
        "is_indexed": local_deal.is_indexed,
        "extracted_text": local_deal.extracted_text,
        "source_onedrive_id": local_deal.source_onedrive_id,
        "source_drive_id": local_deal.source_drive_id,
        "source_email_id": local_deal.source_email_id,
        "processing_status": local_deal.processing_status,
        "processing_error": local_deal.processing_error,
        "request": None,
    }

    additional_contacts = [
        contact_map[str(contact.id)]
        for contact in local_deal.additional_contacts.using(SOURCE_DB).all()
        if str(contact.id) in contact_map
    ]
    payload["other_contacts"] = [str(contact.id) for contact in additional_contacts]

    # Resolve responsibility (Team Members)
    prod_responsibility = []
    for local_profile in local_deal.responsibility.using(SOURCE_DB).all():
        prod_responsibility.append(upsert_user_and_profile(local_profile, dry_run=dry_run))

    if dry_run:
        return prod_deal or local_deal

    if prod_deal:
        changed = []
        for field, value in payload.items():
            current = getattr(prod_deal, field)
            if current != value:
                setattr(prod_deal, field, value)
                changed.append(field)
        if changed:
            prod_deal.save(using=TARGET_DB, update_fields=changed)
        
        # Ensure created_at is preserved for historical deals
        if prod_deal.created_at != local_deal.created_at:
            Deal.objects.using(TARGET_DB).filter(pk=prod_deal.pk).update(created_at=local_deal.created_at)
    else:
        prod_deal = Deal.objects.using(TARGET_DB).create(id=local_deal.id, **payload)
        # Ensure created_at is set correctly for new records
        Deal.objects.using(TARGET_DB).filter(pk=prod_deal.pk).update(created_at=local_deal.created_at)

    prod_deal.additional_contacts.set(additional_contacts)
    prod_deal.responsibility.set(prod_responsibility)
    return prod_deal


def replace_deal_analyses(local_deal: Deal, prod_deal: Deal, dry_run: bool = False):
    analyses = list(local_deal.analyses.using(SOURCE_DB).all().order_by("version", "created_at"))
    if dry_run:
        return len(analyses)

    DealAnalysis.objects.using(TARGET_DB).filter(deal=prod_deal).delete()
    if not analyses:
        return 0

    DealAnalysis.objects.using(TARGET_DB).bulk_create(
        [
            DealAnalysis(
                id=analysis.id,
                deal=prod_deal,
                version=analysis.version,
                analysis_kind=analysis.analysis_kind,
                thinking=analysis.thinking,
                ambiguities=list(analysis.ambiguities or []),
                analysis_json=dict(analysis.analysis_json or {}),
                created_at=analysis.created_at,
            )
            for analysis in analyses
        ],
        batch_size=200,
    )
    return len(analyses)


def replace_phase_logs(local_deal: Deal, prod_deal: Deal, dry_run: bool = False):
    phase_logs = list(local_deal.phase_logs.using(SOURCE_DB).all().order_by("changed_at"))
    if dry_run:
        return len(phase_logs)

    DealPhaseLog.objects.using(TARGET_DB).filter(deal=prod_deal).delete()
    if not phase_logs:
        return 0

    DealPhaseLog.objects.using(TARGET_DB).bulk_create(
        [
            DealPhaseLog(
                id=phase_log.id,
                deal=prod_deal,
                from_phase=phase_log.from_phase,
                to_phase=phase_log.to_phase,
                rationale=phase_log.rationale,
                changed_at=phase_log.changed_at,
                changed_by=None,
            )
            for phase_log in phase_logs
        ],
        batch_size=200,
    )
    return len(phase_logs)


def replace_deal_documents(local_deal: Deal, prod_deal: Deal, dry_run: bool = False):
    documents = list(local_deal.documents.using(SOURCE_DB).all().order_by("created_at"))
    if dry_run:
        return len(documents)

    DealDocument.objects.using(TARGET_DB).filter(deal=prod_deal).delete()
    if not documents:
        return 0

    DealDocument.objects.using(TARGET_DB).bulk_create(
        [
            DealDocument(
                id=document.id,
                deal=prod_deal,
                title=document.title,
                document_type=document.document_type,
                onedrive_id=document.onedrive_id,
                file_url=document.file_url,
                extracted_text=document.extracted_text,
                normalized_text=document.normalized_text,
                evidence_json=dict(document.evidence_json or {}),
                source_map_json=dict(document.source_map_json or {}),
                table_json=list(document.table_json or []),
                key_metrics_json=list(document.key_metrics_json or []),
                reasoning=document.reasoning,
                is_indexed=document.is_indexed,
                is_ai_analyzed=document.is_ai_analyzed,
                initial_analysis_status=document.initial_analysis_status,
                initial_analysis_reason=document.initial_analysis_reason,
                extraction_mode=document.extraction_mode,
                transcription_status=document.transcription_status,
                chunking_status=document.chunking_status,
                last_transcribed_at=document.last_transcribed_at,
                last_chunked_at=document.last_chunked_at,
                created_at=document.created_at,
                uploaded_by=None,
            )
            for document in documents
        ],
        batch_size=200,
    )
    return len(documents)


def replace_deal_chunks(local_deal: Deal, prod_deal: Deal, dry_run: bool = False):
    chunks = list(local_deal.chunks.using(SOURCE_DB).all().order_by("created_at"))
    if dry_run:
        return len(chunks)

    DocumentChunk.objects.using(TARGET_DB).filter(deal=prod_deal).delete()
    if not chunks:
        return 0

    DocumentChunk.objects.using(TARGET_DB).bulk_create(
        [
            DocumentChunk(
                id=chunk.id,
                deal=prod_deal,
                audit_log=None,
                source_type=chunk.source_type,
                source_id=chunk.source_id,
                content=chunk.content,
                embedding=chunk.embedding,
                embedding_model=chunk.embedding_model,
                embedding_dimensions=chunk.embedding_dimensions,
                indexed_at=chunk.indexed_at,
                metadata=dict(chunk.metadata or {}),
                created_at=chunk.created_at,
            )
            for chunk in chunks
        ],
        batch_size=500,
    )
    return len(chunks)


def upsert_audit_log(local_audit_log: AIAuditLog, dry_run: bool = False):
    if dry_run:
        return 1

    payload = {
        "source_type": local_audit_log.source_type,
        "source_id": local_audit_log.source_id,
        "context_label": local_audit_log.context_label,
        "personality": None,
        "skill": None,
        "model_provider": local_audit_log.model_provider,
        "model_used": local_audit_log.model_used,
        "system_prompt": local_audit_log.system_prompt,
        "user_prompt": local_audit_log.user_prompt,
        "raw_response": local_audit_log.raw_response,
        "raw_thinking": local_audit_log.raw_thinking,
        "parsed_json": local_audit_log.parsed_json,
        "request_duration_ms": local_audit_log.request_duration_ms,
        "tokens_used": local_audit_log.tokens_used,
        "source_metadata": local_audit_log.source_metadata,
        "celery_task_id": local_audit_log.celery_task_id,
        "error_message": local_audit_log.error_message,
        "worker_logs": list(local_audit_log.worker_logs or []),
        "is_success": local_audit_log.is_success,
        "status": local_audit_log.status,
        "created_at": local_audit_log.created_at,
    }
    AIAuditLog.objects.using(TARGET_DB).update_or_create(
        id=local_audit_log.id,
        defaults=payload,
    )
    return 1


def sync_referenced_analysis_documents(local_deal: Deal, dry_run: bool = False):
    source_ids = list(
        DocumentChunk.objects.using(SOURCE_DB)
        .filter(deal=local_deal, source_type="analysis_document")
        .exclude(source_id="")
        .values_list("source_id", flat=True)
        .distinct()
    )
    if not source_ids:
        return {"audit_logs": 0, "analysis_documents": 0}

    analysis_documents = list(
        FolderAnalysisDocument.objects.using(SOURCE_DB)
        .filter(id__in=source_ids)
        .select_related("audit_log")
        .order_by("created_at")
    )
    if dry_run:
        audit_log_ids = {str(doc.audit_log_id) for doc in analysis_documents if doc.audit_log_id}
        return {"audit_logs": len(audit_log_ids), "analysis_documents": len(analysis_documents)}

    synced_audit_ids = set()
    for document in analysis_documents:
        if document.audit_log_id and str(document.audit_log_id) not in synced_audit_ids:
            upsert_audit_log(document.audit_log, dry_run=False)
            synced_audit_ids.add(str(document.audit_log_id))

        FolderAnalysisDocument.objects.using(TARGET_DB).update_or_create(
            id=document.id,
            defaults={
                "audit_log_id": document.audit_log_id,
                "source_file_id": document.source_file_id,
                "source_drive_id": document.source_drive_id,
                "file_name": document.file_name,
                "file_path": document.file_path,
                "document_type": document.document_type,
                "raw_extracted_text": document.raw_extracted_text,
                "normalized_text": document.normalized_text,
                "evidence_json": dict(document.evidence_json or {}),
                "source_map_json": dict(document.source_map_json or {}),
                "table_json": list(document.table_json or []),
                "key_metrics_json": list(document.key_metrics_json or []),
                "reasoning": document.reasoning,
                "extraction_mode": document.extraction_mode,
                "transcription_status": document.transcription_status,
                "chunking_status": document.chunking_status,
                "quality_flags": list(document.quality_flags or []),
                "render_metadata": dict(document.render_metadata or {}),
                "is_indexed": document.is_indexed,
                "chunk_count": document.chunk_count,
                "error_message": document.error_message,
                "last_transcribed_at": document.last_transcribed_at,
                "last_chunked_at": document.last_chunked_at,
                "created_at": document.created_at,
                "updated_at": document.updated_at,
            },
        )

    return {"audit_logs": len(synced_audit_ids), "analysis_documents": len(analysis_documents)}


def replace_retrieval_profile(local_deal: Deal, prod_deal: Deal, dry_run: bool = False):
    profile = DealRetrievalProfile.objects.using(SOURCE_DB).filter(deal=local_deal).first()
    if dry_run:
        return int(bool(profile))

    DealRetrievalProfile.objects.using(TARGET_DB).filter(deal=prod_deal).delete()
    if not profile:
        return 0

    DealRetrievalProfile.objects.using(TARGET_DB).create(
        id=profile.id,
        deal=prod_deal,
        profile_text=profile.profile_text,
        embedding=profile.embedding,
        embedding_model=profile.embedding_model,
        embedding_dimensions=profile.embedding_dimensions,
        source_version=profile.source_version,
        metadata=dict(profile.metadata or {}),
        indexed_at=profile.indexed_at,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )
    return 1


def sync_single_deal(local_deal: Deal, dry_run: bool = False):
    related_contacts = {}
    bank_map = {}
    contact_map = {}

    if local_deal.bank_id:
        related_bank = Bank.objects.using(SOURCE_DB).filter(id=local_deal.bank_id).first()
        if related_bank:
            bank_map[str(related_bank.id)] = upsert_bank(related_bank, dry_run=dry_run)

    contact_ids = []
    if local_deal.primary_contact_id:
        contact_ids.append(str(local_deal.primary_contact_id))
    contact_ids.extend(str(contact.id) for contact in local_deal.additional_contacts.using(SOURCE_DB).all())
    for contact_id in dict.fromkeys(contact_ids):
        local_contact = Contact.objects.using(SOURCE_DB).filter(id=contact_id).first()
        if not local_contact:
            continue
        if local_contact.bank_id and str(local_contact.bank_id) not in bank_map:
            related_bank = Bank.objects.using(SOURCE_DB).filter(id=local_contact.bank_id).first()
            if related_bank:
                bank_map[str(related_bank.id)] = upsert_bank(related_bank, dry_run=dry_run)
        related_contacts[contact_id] = local_contact

    for contact_id, local_contact in related_contacts.items():
        contact_map[contact_id] = upsert_contact(local_contact, bank_map, dry_run=dry_run)

    if dry_run:
        prod_deal = upsert_deal(local_deal, bank_map, contact_map, dry_run=True)
        analysis_docs = sync_referenced_analysis_documents(local_deal, dry_run=True)
        return {
            "deal": prod_deal.title if hasattr(prod_deal, "title") else local_deal.title,
            "analyses": replace_deal_analyses(local_deal, local_deal, dry_run=True),
            "phase_logs": replace_phase_logs(local_deal, local_deal, dry_run=True),
            "documents": replace_deal_documents(local_deal, local_deal, dry_run=True),
            "chunks": replace_deal_chunks(local_deal, local_deal, dry_run=True),
            "profile": replace_retrieval_profile(local_deal, local_deal, dry_run=True),
            "audit_logs": analysis_docs["audit_logs"],
            "analysis_documents": analysis_docs["analysis_documents"],
        }

    with transaction.atomic(using=TARGET_DB):
        prod_deal = upsert_deal(local_deal, bank_map, contact_map, dry_run=False)
        analyses = replace_deal_analyses(local_deal, prod_deal, dry_run=False)
        phase_logs = replace_phase_logs(local_deal, prod_deal, dry_run=False)
        documents = replace_deal_documents(local_deal, prod_deal, dry_run=False)
        analysis_docs = sync_referenced_analysis_documents(local_deal, dry_run=False)
        chunks = replace_deal_chunks(local_deal, prod_deal, dry_run=False)
        profile = replace_retrieval_profile(local_deal, prod_deal, dry_run=False)

    return {
        "deal": prod_deal.title,
        "analyses": analyses,
        "phase_logs": phase_logs,
        "documents": documents,
        "chunks": chunks,
        "profile": profile,
        "audit_logs": analysis_docs["audit_logs"],
        "analysis_documents": analysis_docs["analysis_documents"],
    }


def prune_production_deals(local_deals: list[Deal], dry_run: bool = False):
    keep_ids = [deal.id for deal in local_deals]
    queryset = Deal.objects.using(TARGET_DB).exclude(id__in=keep_ids).order_by("title", "id")
    candidates = list(
        queryset.values("id", "title")
        .annotate(
            documents_count=Count("documents", distinct=True),
            chunks_count=Count("chunks", distinct=True),
            analyses_count=Count("analyses", distinct=True),
        )
    )
    if not dry_run and candidates:
        queryset.delete()
    return candidates


def run():
    args = parse_args()
    if args.prune_production_deals and args.deals:
        raise RuntimeError(
            "--prune-production-deals cannot be combined with --deals because it would delete "
            "production deals outside the selected subset. Run a full sync or omit pruning."
        )
    if args.prune_production_deals and args.only_missing_deals:
        raise RuntimeError(
            "--prune-production-deals cannot be combined with --only-missing-deals. "
            "Disable pruning while resuming partial runs."
        )

    prod_database_url = args.prod_database_url
    if not prod_database_url and args.railway_cli:
        prod_database_url = database_url_from_railway_cli(args.railway_project_dir)
    configure_target_database(prod_database_url, ssl_require=args.prod_db_ssl_require)
    compare_schema_state()

    source_vendor = connections[SOURCE_DB].vendor
    target_vendor = connections[TARGET_DB].vendor
    if target_vendor != "postgresql":
        raise RuntimeError(f"Target DB vendor must be postgresql, got {target_vendor}.")

    vector_info = ensure_pgvector(TARGET_DB)
    print(">>> LOCAL TO PROD DB SYNC")
    print(f"Source DB vendor: {source_vendor}")
    print(
        f"Target DB: {vector_info['database']} as {vector_info['user']} "
        f"(pgvector {vector_info['vector_version']})"
    )
    print("-" * 72)

    deals = iter_target_deals(args.deals)
    if not deals:
        print("No matching deals found in local DB.")
        return

    skipped_existing = 0
    if args.only_missing_deals:
        deals, skipped_existing = filter_missing_deals(deals)
        if not deals:
            print("No missing deals to sync. Production already has all selected deals.")
            return

    print(f"Deals selected: {len(deals)}")
    if args.only_missing_deals:
        print(f"Skipped existing production deals: {skipped_existing}")
    if args.dry_run:
        print("Mode: DRY RUN")
    print("-" * 72)

    processed = 0
    errors = 0
    for local_deal in deals:
        try:
            result = sync_single_deal(local_deal, dry_run=args.dry_run)
            processed += 1
            print(
                f"[OK] {result['deal']}: analyses={result['analyses']} "
                f"phase_logs={result['phase_logs']} documents={result['documents']} "
                f"analysis_documents={result['analysis_documents']} chunks={result['chunks']} "
                f"profile={result['profile']}"
            )
        except Exception as exc:
            errors += 1
            print(f"[ERROR] {local_deal.title or local_deal.id}: {exc}")

    if args.prune_production_deals:
        prune_candidates = prune_production_deals(deals, dry_run=args.dry_run)
        print("-" * 72)
        print(
            f"Production prune candidates not present in selected local deal set: {len(prune_candidates)}"
        )
        for candidate in prune_candidates:
            print(
                f"[PRUNE{'-DRY-RUN' if args.dry_run else ''}] {candidate['title']} "
                f"id={candidate['id']} documents={candidate['documents_count']} "
                f"chunks={candidate['chunks_count']} analyses={candidate['analyses_count']}"
            )

    print("-" * 72)
    print(f"Complete. Processed={processed} Errors={errors}")


if __name__ == "__main__":
    run()
