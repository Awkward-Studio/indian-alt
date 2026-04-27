import argparse
import json
import os
import subprocess
import time
import sys
from copy import deepcopy
from itertools import islice
from typing import Iterable

import dj_database_url
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.base")
django.setup()

from django.conf import settings
from django.db import connections, transaction
from django.db.models import Count, Q

from ai_orchestrator.models import AIAuditLog, DealRetrievalProfile, DocumentChunk
from api_requests.models import Request
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
        "--prune-production-data",
        "--prune-production-deals",
        dest="prune_production_data",
        action="store_true",
        help="Delete production rows not present in the selected local sync set. For full deal syncs this prunes deals; for reference-data-only mode this prunes contacts and banks.",
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
    parser.add_argument(
        "--verbose-sync",
        action="store_true",
        help="Print every bank, contact, document, and chunk as it is prepared for sync.",
    )
    parser.add_argument(
        "--progress-interval",
        type=int,
        default=250,
        help="Print progress every N rows during large sync phases. Defaults to 250.",
    )
    parser.add_argument(
        "--reference-data-only",
        action="store_true",
        help="Sync only banks and bankers/contacts. If pruning is enabled, prune production contacts/banks not present locally.",
    )
    parser.add_argument(
        "--skip-reference-data",
        action="store_true",
        help="Skip the initial bank/contact sync phase and go straight to deal data.",
    )
    parser.add_argument(
        "--prune-batch-size",
        type=int,
        default=250,
        help="Delete production prune candidates in batches of this size.",
    )
    parser.add_argument(
        "--prompt-child-overwrite",
        action="store_true",
        help="Prompt per deal whether to rebuild analyses/documents/chunks/profile from local or keep production.",
    )
    parser.add_argument(
        "--interactive-prune",
        action="store_true",
        help="Prompt before deleting each production prune candidate; choose individual deals or prune all.",
    )
    parser.add_argument(
        "--deal-batch-size",
        type=int,
        default=50,
        help="Process local deals in batches of this size with prefetched relations. Defaults to 50.",
    )
    parser.add_argument(
        "--reference-batch-size",
        type=int,
        default=250,
        help="Process reference banks/contacts in batches of this size. Defaults to 250.",
    )
    parser.add_argument(
        "--output-prune-file",
        help="Save production prune candidates to a JSON file instead of deleting them directly.",
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


def request_payload_for(request_obj: Request | None) -> dict | None:
    if not request_obj:
        return None
    return {
        "metadata": deepcopy(request_obj.metadata or {}),
        "body": deepcopy(request_obj.body or {}),
        "attachments": deepcopy(request_obj.attachments or {}),
        "status": request_obj.status,
        "logs": request_obj.logs,
        "created_at": request_obj.created_at,
    }


def related_id_set(objs) -> set[str]:
    return {str(obj.id) for obj in objs if getattr(obj, "id", None)}


def format_profile_debug(profile) -> str:
    if not profile:
        return "N/A"
    pieces = [
        str(getattr(profile, "id", "N/A")),
        normalize_text(getattr(profile, "name", "")) or "N/A",
        normalize_text(getattr(profile, "email", "")) or "N/A",
    ]
    return " | ".join(pieces)


def format_profile_list_debug(profiles) -> str:
    items = [format_profile_debug(profile) for profile in profiles if profile]
    return "; ".join(items) if items else "None"


def deal_state_differences(
    local_deal: Deal,
    prod_deal: Deal | None,
    bank_map: dict[str, Bank],
    contact_map: dict[str, Contact],
) -> list[str]:
    if not prod_deal:
        return ["missing production deal"]

    local_request_payload = request_payload_for(local_deal.request)
    prod_request_payload = request_payload_for(prod_deal.request)

    local_additional_contacts = [
        contact_map[str(contact.id)]
        for contact in local_deal.additional_contacts.using(SOURCE_DB).all()
        if str(contact.id) in contact_map
    ]
    local_responsibility = list(local_deal.responsibility.using(SOURCE_DB).all())
    local_responsibility_mapped = [
        upsert_user_and_profile(profile, dry_run=True) for profile in local_responsibility
    ]
    local_responsibility_ids = related_id_set(local_responsibility_mapped)
    prod_responsibility_ids = related_id_set(prod_deal.responsibility.using(TARGET_DB).all())

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
        "other_contacts": [str(contact.id) for contact in local_additional_contacts],
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
    }

    differences: list[str] = []
    for field, value in payload.items():
        current = getattr(prod_deal, field)
        if current != value:
            differences.append(field)

    if local_request_payload != prod_request_payload:
        differences.append("request")

    prod_additional_ids = related_id_set(prod_deal.additional_contacts.using(TARGET_DB).all())
    if related_id_set(local_additional_contacts) != prod_additional_ids:
        differences.append("additional_contacts")

    if local_responsibility_ids != prod_responsibility_ids:
        differences.append("responsibility")

    return differences


def deal_matches_prod(local_deal: Deal, prod_deal: Deal | None, bank_map: dict[str, Bank], contact_map: dict[str, Contact]) -> bool:
    return not deal_state_differences(local_deal, prod_deal, bank_map, contact_map)


def prompt_child_overwrite(local_deal: Deal, prod_deal: Deal) -> bool:
    if not sys.stdin or not sys.stdin.isatty():
        return True

    prompt = (
        f"\n[CHILD-OVERWRITE] {local_deal.title or local_deal.id}\n"
        f"  production_deal_id={prod_deal.id}\n"
        "  Rewrite analyses/documents/chunks/profile from local?\n"
        "  [y] yes  [n] keep production  [s] skip deal\n"
        "  Choice: "
    )
    while True:
        answer = input(prompt).strip().lower()
        if answer in {"y", "yes", ""}:
            return True
        if answer in {"n", "no"}:
            return False
        if answer in {"s", "skip"}:
            raise RuntimeError(f"Skipped by operator: {local_deal.title or local_deal.id}")
        print("Please respond with y, n, or s.", flush=True)


def prompt_prune_selection(candidates: list[dict], interactive: bool) -> list[dict]:
    if not candidates:
        return []
    if not interactive:
        return candidates
    if not sys.stdin or not sys.stdin.isatty():
        return candidates

    print("\n[PRUNE-INTERACTIVE] Production deals eligible for deletion:", flush=True)
    for index, candidate in enumerate(candidates, start=1):
        print(
            f"  {index}. {candidate['title'] or candidate['id']} "
            f"id={candidate['id']} documents={candidate['documents_count']} "
            f"chunks={candidate['chunks_count']} analyses={candidate['analyses_count']}",
            flush=True,
        )
    print("Choose: comma-separated numbers, 'all', or 'skip'.", flush=True)

    while True:
        answer = input("Deletion selection: ").strip().lower()
        if answer in {"all", "a"}:
            return candidates
        if answer in {"skip", "s", ""}:
            return []
        indices = []
        try:
            for part in answer.split(","):
                part = part.strip()
                if not part:
                    continue
                indices.append(int(part))
        except ValueError:
            print("Please enter comma-separated numbers, 'all', or 'skip'.", flush=True)
            continue
        selected = []
        invalid = False
        for index in indices:
            if index < 1 or index > len(candidates):
                invalid = True
                break
            selected.append(candidates[index - 1])
        if invalid:
            print("One or more numbers were out of range.", flush=True)
            continue
        return selected


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


def deal_batches(deals: list[Deal], batch_size: int):
    batch_size = max(1, int(batch_size))
    iterator = iter(deals)
    while True:
        batch = list(islice(iterator, batch_size))
        if not batch:
            break
        yield batch


def hydrate_local_deal_batch(batch: list[Deal]) -> list[Deal]:
    if not batch:
        return []
    deal_ids = [deal.id for deal in batch]
    queryset = (
        Deal.objects.using(SOURCE_DB)
        .filter(id__in=deal_ids)
        .select_related("request", "bank", "primary_contact")
        .prefetch_related("analyses", "phase_logs", "documents", "chunks", "additional_contacts", "responsibility")
    )
    hydrated_by_id = {deal.id: deal for deal in queryset}
    return [hydrated_by_id[deal_id] for deal_id in deal_ids if deal_id in hydrated_by_id]


def iter_batches(items, batch_size: int):
    batch_size = max(1, int(batch_size))
    iterator = iter(items)
    while True:
        batch = list(islice(iterator, batch_size))
        if not batch:
            break
        yield batch


def upsert_request(local_request: Request | None, dry_run: bool = False, verbose: bool = False) -> Request | None:
    if not local_request:
        return None

    prod_request = Request.objects.using(TARGET_DB).filter(id=local_request.id).first()
    payload = request_payload_for(local_request)

    if dry_run:
        return prod_request or local_request

    if prod_request:
        if request_payload_for(prod_request) == payload:
            if verbose:
                print(f"[SKIP-UNCHANGED] REQUEST {local_request.id}", flush=True)
            return prod_request
        changed = []
        for field, value in payload.items():
            current = getattr(prod_request, field)
            if current != value:
                setattr(prod_request, field, value)
                changed.append(field)
        if changed:
            prod_request.save(using=TARGET_DB, update_fields=changed)
            if verbose:
                print(f"[UPDATE] REQUEST {local_request.id} fields={','.join(changed)}", flush=True)
        return prod_request

    prod_request = Request.objects.using(TARGET_DB).create(id=local_request.id, **payload)
    if prod_request.created_at != local_request.created_at:
        Request.objects.using(TARGET_DB).filter(pk=prod_request.pk).update(created_at=local_request.created_at)
    if verbose:
        print(f"[CREATE] REQUEST {local_request.id}", flush=True)
    return prod_request


def filter_missing_deals(local_deals: list[Deal]) -> tuple[list[Deal], int]:
    if not local_deals:
        return [], 0

    local_id_set = {str(local_deal.id) for local_deal in local_deals}
    local_title_set = {
        normalize_text(local_deal.title).lower()
        for local_deal in local_deals
        if normalize_text(local_deal.title)
    }

    prod_id_set = {
        str(value)
        for value in Deal.objects.using(TARGET_DB)
        .filter(id__in=list(local_id_set))
        .values_list("id", flat=True)
    }
    prod_title_set = {
        normalize_text(value).lower()
        for value in Deal.objects.using(TARGET_DB).values_list("title", flat=True)
        if normalize_text(value)
    }

    filtered: list[Deal] = []
    skipped_existing = 0
    for local_deal in local_deals:
        title_key = normalize_text(local_deal.title).lower()
        exists = str(local_deal.id) in prod_id_set or (title_key and title_key in prod_title_set)
        if exists:
            skipped_existing += 1
        else:
            filtered.append(local_deal)
    return filtered, skipped_existing


def _deal_rank_key_for_sync(deal: Deal):
    document_count = deal.documents.using(SOURCE_DB).count()
    chunk_count = deal.chunks.using(SOURCE_DB).count()
    analysis_count = deal.analyses.using(SOURCE_DB).count()
    created_at = deal.created_at.isoformat() if deal.created_at else ""
    return (-document_count, -chunk_count, -analysis_count, created_at, str(deal.id))


def dedupe_local_deals_by_title(local_deals: list[Deal]) -> tuple[list[Deal], list[dict]]:
    grouped: dict[str, list[Deal]] = {}
    passthrough: list[Deal] = []

    for deal in local_deals:
        normalized_title = " ".join(normalize_text(deal.title).lower().split())
        if not normalized_title:
            passthrough.append(deal)
            continue
        grouped.setdefault(normalized_title, []).append(deal)

    deduped: list[Deal] = list(passthrough)
    collisions: list[dict] = []

    for normalized_title, deals in grouped.items():
        if len(deals) == 1:
            deduped.append(deals[0])
            continue

        ranked = sorted(deals, key=_deal_rank_key_for_sync)
        winner = ranked[0]
        dropped = ranked[1:]
        deduped.append(winner)
        collisions.append(
            {
                "normalized_title": normalized_title,
                "winner": winner,
                "dropped": dropped,
            }
        )

    deduped.sort(key=lambda deal: ((deal.title or "").lower(), deal.created_at or ""))
    return deduped, collisions


def upsert_bank(local_bank: Bank | None, dry_run: bool = False, verbose: bool = False) -> Bank | None:
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
        if (
            prod_bank.name == payload["name"]
            and prod_bank.website_domain == payload["website_domain"]
            and prod_bank.description == payload["description"]
        ):
            if verbose:
                print(f"[SKIP-UNCHANGED] BANK {local_bank.name or local_bank.id}", flush=True)
            return prod_bank
        changed = []
        for field, value in payload.items():
            if getattr(prod_bank, field) != value:
                setattr(prod_bank, field, value)
                changed.append(field)
        if changed:
            prod_bank.save(using=TARGET_DB, update_fields=changed)
            if verbose:
                print(
                    f"[UPDATE] BANK {local_bank.name or local_bank.id} fields={','.join(changed)}",
                    flush=True,
                )
        return prod_bank

    if verbose:
        print(f"[CREATE] BANK {local_bank.name or local_bank.id}", flush=True)
    return Bank.objects.using(TARGET_DB).create(id=local_bank.id, **payload)


def upsert_contact(
    local_contact: Contact | None,
    bank_map: dict[str, Bank],
    dry_run: bool = False,
    verbose: bool = False,
) -> Contact | None:
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
        if all(getattr(prod_contact, field) == value for field, value in payload.items()):
            if verbose:
                print(f"[SKIP-UNCHANGED] CONTACT {local_contact.name or local_contact.id}", flush=True)
            return prod_contact
        changed = []
        for field, value in payload.items():
            current = getattr(prod_contact, field)
            if current != value:
                setattr(prod_contact, field, value)
                changed.append(field)
        if changed:
            prod_contact.save(using=TARGET_DB, update_fields=changed)
            if verbose:
                print(
                    f"[UPDATE] CONTACT {local_contact.name or local_contact.id} fields={','.join(changed)}",
                    flush=True,
                )
        return prod_contact

    if verbose:
        print(f"[CREATE] CONTACT {local_contact.name or local_contact.id}", flush=True)
    return Contact.objects.using(TARGET_DB).create(id=local_contact.id, **payload)


def sync_reference_data(
    dry_run: bool = False,
    verbose: bool = False,
    progress_interval: int = 250,
    batch_size: int = 250,
) -> dict:
    bank_map: dict[str, Bank] = {}
    contact_map: dict[str, Contact] = {}

    local_banks = list(Bank.objects.using(SOURCE_DB).all().order_by("name", "id"))
    local_contacts = list(Contact.objects.using(SOURCE_DB).select_related("bank").all().order_by("name", "id"))

    def perform_sync():
        print(f"[REFERENCE] Starting banks: total={len(local_banks)} batch_size={batch_size}", flush=True)
        bank_index = 0
        for batch_number, bank_batch in enumerate(iter_batches(local_banks, batch_size), start=1):
            print(
                f"[REFERENCE-BATCH] banks batch={batch_number} size={len(bank_batch)}",
                flush=True,
            )
            for local_bank in bank_batch:
                bank_index += 1
                if verbose:
                    print(
                        f"[BANK {bank_index}/{len(local_banks)}] {local_bank.name or local_bank.id} "
                        f"domain={local_bank.website_domain or 'N/A'}",
                        flush=True,
                    )
                synced_bank = upsert_bank(local_bank, dry_run=dry_run, verbose=verbose)
                if synced_bank:
                    bank_map[str(local_bank.id)] = synced_bank
                if progress_interval > 0 and bank_index % progress_interval == 0:
                    print(f"[REFERENCE] banks synced={bank_index}/{len(local_banks)}", flush=True)

        print(f"[REFERENCE] Starting bankers/contacts: total={len(local_contacts)} batch_size={batch_size}", flush=True)
        contact_index = 0
        for batch_number, contact_batch in enumerate(iter_batches(local_contacts, batch_size), start=1):
            print(
                f"[REFERENCE-BATCH] contacts batch={batch_number} size={len(contact_batch)}",
                flush=True,
            )
            for local_contact in contact_batch:
                contact_index += 1
                if verbose:
                    print(
                        f"[CONTACT {contact_index}/{len(local_contacts)}] {local_contact.name or local_contact.id} "
                        f"email={local_contact.email or 'N/A'} bank_id={local_contact.bank_id or 'N/A'}",
                        flush=True,
                    )
                synced_contact = upsert_contact(local_contact, bank_map, dry_run=dry_run, verbose=verbose)
                if synced_contact:
                    contact_map[str(local_contact.id)] = synced_contact
                if progress_interval > 0 and contact_index % progress_interval == 0:
                    print(f"[REFERENCE] contacts synced={contact_index}/{len(local_contacts)}", flush=True)

    if dry_run:
        perform_sync()
    else:
        with transaction.atomic(using=TARGET_DB):
            perform_sync()

    return {
        "bank_map": bank_map,
        "contact_map": contact_map,
        "banks": len(bank_map),
        "contacts": len(contact_map),
        "local_banks": len(local_banks),
        "local_contacts": len(local_contacts),
    }


def load_existing_reference_maps(verbose: bool = False, progress_interval: int = 250, batch_size: int = 250) -> dict:
    bank_map: dict[str, Bank] = {}
    contact_map: dict[str, Contact] = {}

    local_banks = list(Bank.objects.using(SOURCE_DB).all().order_by("name", "id"))
    local_contacts = list(Contact.objects.using(SOURCE_DB).select_related("bank").all().order_by("name", "id"))

    print(
        f"[REFERENCE-MAP] Loading existing production banks: total={len(local_banks)} batch_size={batch_size}",
        flush=True,
    )
    bank_index = 0
    for batch_number, bank_batch in enumerate(iter_batches(local_banks, batch_size), start=1):
        print(f"[REFERENCE-MAP-BATCH] banks batch={batch_number} size={len(bank_batch)}", flush=True)
        for local_bank in bank_batch:
            bank_index += 1
            prod_bank = Bank.objects.using(TARGET_DB).filter(id=local_bank.id).first()
            if not prod_bank and normalize_text(local_bank.website_domain):
                prod_bank = Bank.objects.using(TARGET_DB).filter(
                    website_domain__iexact=local_bank.website_domain
                ).first()
            if not prod_bank and normalize_text(local_bank.name):
                prod_bank = Bank.objects.using(TARGET_DB).filter(name__iexact=local_bank.name).first()
            if prod_bank:
                bank_map[str(local_bank.id)] = prod_bank
            if verbose:
                print(
                    f"[BANK-MAP {bank_index}/{len(local_banks)}] {local_bank.name or local_bank.id} "
                    f"mapped={'yes' if prod_bank else 'no'}",
                    flush=True,
                )
            elif progress_interval > 0 and bank_index % progress_interval == 0:
                print(f"[REFERENCE-MAP] banks checked={bank_index}/{len(local_banks)} mapped={len(bank_map)}", flush=True)

    print(
        f"[REFERENCE-MAP] Loading existing production contacts: total={len(local_contacts)} batch_size={batch_size}",
        flush=True,
    )
    contact_index = 0
    for batch_number, contact_batch in enumerate(iter_batches(local_contacts, batch_size), start=1):
        print(f"[REFERENCE-MAP-BATCH] contacts batch={batch_number} size={len(contact_batch)}", flush=True)
        for local_contact in contact_batch:
            contact_index += 1
            prod_contact = Contact.objects.using(TARGET_DB).filter(id=local_contact.id).first()
            if not prod_contact and normalize_text(local_contact.email):
                prod_contact = Contact.objects.using(TARGET_DB).filter(email__iexact=local_contact.email).first()
            if not prod_contact and normalize_text(local_contact.name):
                query = Contact.objects.using(TARGET_DB).filter(name__iexact=local_contact.name)
                mapped_bank = bank_map.get(str(local_contact.bank_id)) if local_contact.bank_id else None
                if mapped_bank:
                    query = query.filter(bank=mapped_bank)
                prod_contact = query.first()
            if prod_contact:
                contact_map[str(local_contact.id)] = prod_contact
            if verbose:
                print(
                    f"[CONTACT-MAP {contact_index}/{len(local_contacts)}] {local_contact.name or local_contact.id} "
                    f"mapped={'yes' if prod_contact else 'no'}",
                    flush=True,
                )
            elif progress_interval > 0 and contact_index % progress_interval == 0:
                print(f"[REFERENCE-MAP] contacts checked={contact_index}/{len(local_contacts)} mapped={len(contact_map)}", flush=True)

    return {
        "bank_map": bank_map,
        "contact_map": contact_map,
        "banks": len(bank_map),
        "contacts": len(contact_map),
        "local_banks": len(local_banks),
        "local_contacts": len(local_contacts),
    }


def prune_production_contacts(contact_map: dict[str, Contact], dry_run: bool = False):
    keep_ids = [contact.id for contact in contact_map.values() if getattr(contact, "id", None)]
    queryset = Contact.objects.using(TARGET_DB).exclude(id__in=keep_ids).order_by("name", "id")
    candidates = list(
        queryset.values("id", "name", "email", "bank_id")
    )
    if not dry_run and candidates:
        queryset.delete()
    return candidates


def prune_production_banks(bank_map: dict[str, Bank], dry_run: bool = False):
    keep_ids = [bank.id for bank in bank_map.values() if getattr(bank, "id", None)]
    queryset = Bank.objects.using(TARGET_DB).exclude(id__in=keep_ids).order_by("name", "id")
    candidates = list(
        queryset.values("id", "name", "website_domain")
    )
    if not dry_run and candidates:
        queryset.delete()
    return candidates


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

def upsert_deal(
    local_deal: Deal,
    bank_map: dict[str, Bank],
    contact_map: dict[str, Contact],
    dry_run: bool = False,
    verbose: bool = False,
) -> Deal:
    prod_deal = Deal.objects.using(TARGET_DB).filter(id=local_deal.id).first()
    if not prod_deal and normalize_text(local_deal.title):
        prod_deal = Deal.objects.using(TARGET_DB).filter(title__iexact=local_deal.title).first()
    local_request = local_deal.request
    local_request_payload = request_payload_for(local_request)

    local_additional_contacts = [
        contact
        for contact in local_deal.additional_contacts.using(SOURCE_DB).all()
    ]
    additional_contacts = [
        contact_map[str(contact.id)]
        for contact in local_additional_contacts
        if str(contact.id) in contact_map
    ]
    local_additional_contact_ids = related_id_set(local_additional_contacts)
    additional_contact_ids = related_id_set(additional_contacts)

    local_responsibility = list(local_deal.responsibility.using(SOURCE_DB).all())
    local_responsibility_ids = related_id_set(
        [upsert_user_and_profile(profile, dry_run=True) for profile in local_responsibility]
    )
    prod_responsibility_ids = related_id_set(prod_deal.responsibility.using(TARGET_DB).all()) if prod_deal else set()

    mapped_bank = bank_map.get(str(local_deal.bank_id)) if local_deal.bank_id else None
    mapped_primary_contact = contact_map.get(str(local_deal.primary_contact_id)) if local_deal.primary_contact_id else None
    prod_request_payload = request_payload_for(prod_deal.request) if prod_deal else None

    payload = {
        "title": local_deal.title,
        "bank": mapped_bank,
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
        "other_contacts": [str(contact.id) for contact in additional_contacts],
        "primary_contact": mapped_primary_contact,
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
        "request": prod_deal.request if prod_deal else None,
    }

    if prod_deal:
        scalar_matches = all(getattr(prod_deal, field) == value for field, value in payload.items() if field != "request")
        request_matches = local_request_payload == prod_request_payload
        additional_matches = local_additional_contact_ids == related_id_set(prod_deal.additional_contacts.using(TARGET_DB).all())
        responsibility_matches = local_responsibility_ids == prod_responsibility_ids
        if scalar_matches and request_matches and additional_matches and responsibility_matches:
            if verbose:
                print(f"[SKIP-UNCHANGED] DEAL {local_deal.title or local_deal.id}", flush=True)
            return prod_deal if not dry_run else local_deal

    prod_request = upsert_request(local_request, dry_run=dry_run, verbose=verbose)
    payload["request"] = prod_request

    # Resolve responsibility (Team Members)
    prod_responsibility = []
    for local_profile in local_responsibility:
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
    if verbose:
        print(f"[UPDATE] DEAL {local_deal.title or local_deal.id}", flush=True)
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


def replace_deal_documents(
    local_deal: Deal,
    prod_deal: Deal,
    dry_run: bool = False,
    verbose: bool = False,
    progress_interval: int = 250,
):
    documents = list(local_deal.documents.using(SOURCE_DB).all().order_by("created_at"))
    if dry_run:
        if verbose:
            for index, document in enumerate(documents, start=1):
                print(f"[DOCUMENT-DRY-RUN {index}/{len(documents)}] {local_deal.title}: {document.title or document.id}", flush=True)
        return len(documents)

    print(f"[DOCUMENTS] {local_deal.title}: replacing {len(documents)} documents", flush=True)
    DealDocument.objects.using(TARGET_DB).filter(deal=prod_deal).delete()
    if not documents:
        return 0
    if verbose:
        for index, document in enumerate(documents, start=1):
            print(
                f"[DOCUMENT {index}/{len(documents)}] {local_deal.title}: "
                f"{document.title or document.id} type={document.document_type or 'N/A'}",
                flush=True,
            )
    elif progress_interval > 0 and len(documents) >= progress_interval:
        print(f"[DOCUMENTS] {local_deal.title}: preparing bulk insert for {len(documents)} documents", flush=True)

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


def replace_deal_chunks(
    local_deal: Deal,
    prod_deal: Deal,
    dry_run: bool = False,
    verbose: bool = False,
    progress_interval: int = 250,
):
    chunks = list(local_deal.chunks.using(SOURCE_DB).all().order_by("created_at"))
    if dry_run:
        if verbose:
            for index, chunk in enumerate(chunks, start=1):
                print(
                    f"[CHUNK-DRY-RUN {index}/{len(chunks)}] {local_deal.title}: "
                    f"source={chunk.source_type}:{chunk.source_id or 'N/A'}",
                    flush=True,
                )
        return len(chunks)

    print(f"[CHUNKS] {local_deal.title}: replacing {len(chunks)} chunks", flush=True)
    DocumentChunk.objects.using(TARGET_DB).filter(deal=prod_deal).delete()
    if not chunks:
        return 0
    if verbose:
        for index, chunk in enumerate(chunks, start=1):
            print(
                f"[CHUNK {index}/{len(chunks)}] {local_deal.title}: "
                f"source={chunk.source_type}:{chunk.source_id or 'N/A'} "
                f"embedding_dims={chunk.embedding_dimensions or 'N/A'}",
                flush=True,
            )
    elif progress_interval > 0 and len(chunks) >= progress_interval:
        print(f"[CHUNKS] {local_deal.title}: preparing bulk insert for {len(chunks)} chunks", flush=True)

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


def sync_single_deal(
    local_deal: Deal,
    bank_map: dict[str, Bank],
    contact_map: dict[str, Contact],
    dry_run: bool = False,
    verbose: bool = False,
    progress_interval: int = 250,
    prompt_child_overwrite_enabled: bool = False,
):
    print(f"[DEAL] {local_deal.title or local_deal.id}: starting", flush=True)
    print(
        f"[DEAL-SUMMARY] id={local_deal.id} bank_id={local_deal.bank_id or 'N/A'} "
        f"primary_contact_id={local_deal.primary_contact_id or 'N/A'} "
        f"additional_contacts={local_deal.additional_contacts.count()} request_id={local_deal.request_id or 'N/A'} "
        f"analyses={local_deal.analyses.count()} documents={local_deal.documents.count()} "
        f"chunks={local_deal.chunks.count()} phase_logs={local_deal.phase_logs.count()}",
        flush=True,
    )
    if dry_run:
        prod_deal = upsert_deal(local_deal, bank_map, contact_map, dry_run=True, verbose=verbose)
        analysis_docs = sync_referenced_analysis_documents(local_deal, dry_run=True)
        return {
            "deal": prod_deal.title if hasattr(prod_deal, "title") else local_deal.title,
            "analyses": replace_deal_analyses(local_deal, local_deal, dry_run=True),
            "phase_logs": replace_phase_logs(local_deal, local_deal, dry_run=True),
            "documents": replace_deal_documents(
                local_deal,
                local_deal,
                dry_run=True,
                verbose=verbose,
                progress_interval=progress_interval,
            ),
            "chunks": replace_deal_chunks(
                local_deal,
                local_deal,
                dry_run=True,
                verbose=verbose,
                progress_interval=progress_interval,
            ),
            "profile": replace_retrieval_profile(local_deal, local_deal, dry_run=True),
            "audit_logs": analysis_docs["audit_logs"],
            "analysis_documents": analysis_docs["analysis_documents"],
        }

    existing_prod_deal = Deal.objects.using(TARGET_DB).filter(id=local_deal.id).first()
    if not existing_prod_deal and normalize_text(local_deal.title):
        existing_prod_deal = Deal.objects.using(TARGET_DB).filter(title__iexact=local_deal.title).first()

    with transaction.atomic(using=TARGET_DB):
        print(f"[DEAL] {local_deal.title or local_deal.id}: upserting deal row", flush=True)
        prod_deal = upsert_deal(local_deal, bank_map, contact_map, dry_run=False, verbose=verbose)
        rewrite_children = True
        differences = deal_state_differences(local_deal, existing_prod_deal, bank_map, contact_map)
        link_only_changes = bool(differences) and set(differences).issubset(
            {"bank", "primary_contact", "additional_contacts"}
        )
        if prompt_child_overwrite_enabled and not differences:
            rewrite_children = False
        elif prompt_child_overwrite_enabled and link_only_changes:
            print(
                f"[DEAL] {local_deal.title or local_deal.id}: link-only changes detected; "
                f"auto-accepting local child tables",
                flush=True,
            )
            rewrite_children = True
        elif prompt_child_overwrite_enabled:
            print(
                f"[DEAL] {local_deal.title or local_deal.id}: changed_fields={', '.join(differences) if differences else 'unknown'}",
                flush=True,
            )
            if "responsibility" in differences:
                local_responsibility_debug = format_profile_list_debug(
                    local_deal.responsibility.using(SOURCE_DB).all()
                )
                prod_responsibility_debug = format_profile_list_debug(
                    prod_deal.responsibility.using(TARGET_DB).all()
                )
                print(
                    f"[DEAL] {local_deal.title or local_deal.id}: responsibility local=[{local_responsibility_debug}] "
                    f"prod=[{prod_responsibility_debug}]",
                    flush=True,
                )
            rewrite_children = prompt_child_overwrite(local_deal, prod_deal)

        if rewrite_children:
            print(f"[DEAL] {local_deal.title or local_deal.id}: rebuilding child tables from local", flush=True)
            analyses = replace_deal_analyses(local_deal, prod_deal, dry_run=False)
            print(f"[DEAL] {local_deal.title or local_deal.id}: replacing phase logs", flush=True)
            phase_logs = replace_phase_logs(local_deal, prod_deal, dry_run=False)
            documents = replace_deal_documents(
                local_deal,
                prod_deal,
                dry_run=False,
                verbose=verbose,
                progress_interval=progress_interval,
            )
            print(f"[DEAL] {local_deal.title or local_deal.id}: syncing referenced analysis documents", flush=True)
            analysis_docs = sync_referenced_analysis_documents(local_deal, dry_run=False)
            chunks = replace_deal_chunks(
                local_deal,
                prod_deal,
                dry_run=False,
                verbose=verbose,
                progress_interval=progress_interval,
            )
            print(f"[DEAL] {local_deal.title or local_deal.id}: replacing retrieval profile", flush=True)
            profile = replace_retrieval_profile(local_deal, prod_deal, dry_run=False)
        else:
            print(
                f"[DEAL] {local_deal.title or local_deal.id}: keeping production analyses/documents/chunks/profile",
                flush=True,
            )
            analyses = prod_deal.analyses.using(TARGET_DB).count()
            phase_logs = prod_deal.phase_logs.using(TARGET_DB).count()
            documents = prod_deal.documents.using(TARGET_DB).count()
            chunks = prod_deal.chunks.using(TARGET_DB).count()
            profile = int(DealRetrievalProfile.objects.using(TARGET_DB).filter(deal=prod_deal).exists())
            analysis_docs = {"audit_logs": 0, "analysis_documents": 0}

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


def prune_production_deals(
    local_deals: list[Deal],
    dry_run: bool = False,
    batch_size: int = 250,
    interactive: bool = False,
    output_file: str = None,
):
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

    if output_file and candidates:
        output_data = {
            "timestamp": time.time(),
            "target_db": TARGET_DB,
            "to_delete_ids": [str(c["id"]) for c in candidates],
            "summary": {"deals": len(candidates)}
        }
        with open(output_file, 'w') as f:
            json.dump(output_data, f, indent=2)
        print(f"[PRUNE] Saved {len(candidates)} pruning candidates to {output_file}", flush=True)
        return candidates

    if dry_run or not candidates:
        return candidates

    candidates = prompt_prune_selection(candidates, interactive=interactive)
    if not candidates:
        print("[PRUNE] No production deals selected for deletion.", flush=True)
        return []

    effective_batch_size = max(1, int(batch_size))
    for start in range(0, len(candidates), effective_batch_size):
        batch = candidates[start : start + effective_batch_size]
        batch_ids = [candidate["id"] for candidate in batch]
        batch_titles = ", ".join((candidate["title"] or str(candidate["id"])) for candidate in batch[:3])
        if len(batch) > 3:
            batch_titles += f", ... (+{len(batch) - 3} more)"
        print(
            f"[PRUNE-BATCH] deleting {len(batch)} production deals "
            f"({start + 1}-{start + len(batch)} of {len(candidates)}): {batch_titles}",
            flush=True,
        )
        with transaction.atomic(using=TARGET_DB):
            deleted_count, deleted_by_model = Deal.objects.using(TARGET_DB).filter(id__in=batch_ids).delete()
        print(
            f"[PRUNE-BATCH] deleted_count={deleted_count} model_breakdown={deleted_by_model}",
            flush=True,
        )
    return candidates


def run():
    args = parse_args()
    if args.prune_production_data and args.deals:
        raise RuntimeError(
            "--prune-production-data cannot be combined with --deals because it would delete "
            "production deals outside the selected subset. Run a full sync or omit pruning."
        )
    if args.prune_production_data and args.only_missing_deals:
        raise RuntimeError(
            "--prune-production-data cannot be combined with --only-missing-deals. "
            "Disable pruning while resuming partial runs."
        )
    if args.reference_data_only and args.deals:
        raise RuntimeError(
            "--reference-data-only cannot be combined with --deals. It always syncs the full local bank/contact set."
        )
    if args.reference_data_only and args.only_missing_deals:
        raise RuntimeError(
            "--reference-data-only cannot be combined with --only-missing-deals."
        )
    if args.reference_data_only and args.skip_reference_data:
        raise RuntimeError(
            "--reference-data-only cannot be combined with --skip-reference-data."
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

    deals, title_collisions = dedupe_local_deals_by_title(deals)
    if title_collisions:
        print(f"Collapsed duplicate local deal titles: {len(title_collisions)}")
        for collision in title_collisions:
            winner = collision["winner"]
            dropped = collision["dropped"]
            dropped_labels = ", ".join(str(item.id) for item in dropped)
            print(
                f"[TITLE-DUPE] kept={winner.title or winner.id} winner_id={winner.id} "
                f"dropped_ids=[{dropped_labels}]"
            )

    skipped_existing = 0
    if args.only_missing_deals:
        print("Resolving missing deals against production...")
        t0 = time.time()
        deals, skipped_existing = filter_missing_deals(deals)
        t1 = time.time()
        if not deals:
            print("No missing deals to sync. Production already has all selected deals.")
            return
        print(f"Missing-deal resolution completed in {round(t1 - t0, 2)}s")

    print(f"Deals selected: {len(deals)}")
    if args.only_missing_deals:
        print(f"Skipped existing production deals: {skipped_existing}")
    if args.dry_run:
        print("Mode: DRY RUN")
    print("-" * 72)

    reference_data = {"bank_map": {}, "contact_map": {}, "banks": 0, "contacts": 0, "local_banks": 0, "local_contacts": 0}
    if args.skip_reference_data:
        print("Skipping reference data writes: loading existing production bank/contact maps.", flush=True)
        t0 = time.time()
        reference_data = load_existing_reference_maps(
            verbose=args.verbose_sync,
            progress_interval=args.progress_interval,
            batch_size=args.reference_batch_size,
        )
        t1 = time.time()
        print(
            f"[REFERENCE-MAP] banks={reference_data['banks']}/{reference_data['local_banks']} "
            f"contacts={reference_data['contacts']}/{reference_data['local_contacts']} "
            f"elapsed={round(t1 - t0, 2)}s",
            flush=True,
        )
        print("-" * 72)
    else:
        print("Syncing reference data: banks and bankers/contacts...")
        t0 = time.time()
        reference_data = sync_reference_data(
            dry_run=args.dry_run,
            verbose=args.verbose_sync,
            progress_interval=args.progress_interval,
            batch_size=args.reference_batch_size,
        )
        t1 = time.time()
        print(
            f"[REFERENCE] banks={reference_data['banks']}/{reference_data['local_banks']} "
            f"contacts={reference_data['contacts']}/{reference_data['local_contacts']} "
            f"elapsed={round(t1 - t0, 2)}s"
        )
        print("-" * 72)

    if args.reference_data_only:
        if args.prune_production_data:
            print("Pruning production contacts not present in local reference data...", flush=True)
            contact_prune_candidates = prune_production_contacts(
                reference_data["contact_map"],
                dry_run=args.dry_run,
            )
            print(
                f"Production contact prune candidates not present in local reference set: {len(contact_prune_candidates)}",
                flush=True,
            )
            if args.verbose_sync:
                for candidate in contact_prune_candidates:
                    print(
                        f"[CONTACT-PRUNE{'-DRY-RUN' if args.dry_run else ''}] "
                        f"{candidate['name'] or candidate['id']} "
                        f"email={candidate['email'] or 'N/A'} bank_id={candidate['bank_id'] or 'N/A'}",
                        flush=True,
                    )
            print("Pruning production banks not present in local reference data...", flush=True)
            bank_prune_candidates = prune_production_banks(
                reference_data["bank_map"],
                dry_run=args.dry_run,
            )
            print(
                f"Production bank prune candidates not present in local reference set: {len(bank_prune_candidates)}",
                flush=True,
            )
            if args.verbose_sync:
                for candidate in bank_prune_candidates:
                    print(
                        f"[BANK-PRUNE{'-DRY-RUN' if args.dry_run else ''}] "
                        f"{candidate['name'] or candidate['id']} "
                        f"domain={candidate['website_domain'] or 'N/A'}",
                        flush=True,
                    )
        print("Reference-data-only sync completed.", flush=True)
        return

    processed = 0
    errors = 0
    deal_batch_size = max(1, int(args.deal_batch_size))
    for batch_index, local_deal_batch in enumerate(deal_batches(deals, deal_batch_size), start=1):
        hydrated_batch = hydrate_local_deal_batch(local_deal_batch)
        print(
            f"[DEAL-BATCH] {batch_index}: size={len(hydrated_batch)} "
            f"batch_size={deal_batch_size} first={hydrated_batch[0].title if hydrated_batch else 'N/A'}",
            flush=True,
        )
        for local_deal in hydrated_batch:
            try:
                result = sync_single_deal(
                    local_deal,
                    reference_data["bank_map"],
                    reference_data["contact_map"],
                    dry_run=args.dry_run,
                    verbose=args.verbose_sync,
                    progress_interval=args.progress_interval,
                    prompt_child_overwrite_enabled=args.prompt_child_overwrite,
                )
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

    if args.prune_production_data:
        prune_candidates = prune_production_deals(
            deals,
            dry_run=args.dry_run,
            batch_size=args.prune_batch_size,
            interactive=args.interactive_prune,
            output_file=args.output_prune_file,
        )
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
