from __future__ import annotations

import argparse
import os
import subprocess
from copy import deepcopy
from collections import defaultdict
from typing import Iterable

import dj_database_url
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connections, transaction

from deals.models import Deal


SOURCE_DB = "default"
TARGET_DB = "production"


def normalize_text(value) -> str:
    if value is None:
        return ""
    return " ".join(str(value).strip().split())


def resolve_ssl_require(cli_value: bool | None) -> bool:
    if cli_value is not None:
        return cli_value
    raw_value = os.environ.get("PROD_DB_SSL_REQUIRE", os.environ.get("DB_SSL_REQUIRE", "true"))
    return raw_value.strip().lower() not in {"0", "false", "no", "off"}


def database_url_from_railway_cli(project_dir: str = ".") -> str | None:
    command = ["railway", "variables", "--json"]
    result = subprocess.run(
        command,
        cwd=project_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = {}
    if result.stdout:
        import json

        payload = json.loads(result.stdout)
    preferred_keys = (
        "DATABASE_PUBLIC_URL",
        "DATABASE_URL_PUBLIC",
        "POSTGRES_PUBLIC_URL",
        "DATABASE_URL",
    )
    for key in preferred_keys:
        value = payload.get(key)
        if value:
            return value
    return None


def configure_target_database(prod_database_url: str, ssl_require: bool = True) -> None:
    if not prod_database_url:
        raise CommandError("A production database URL is required.")
    source_config = settings.DATABASES[SOURCE_DB]
    parsed = dj_database_url.parse(prod_database_url, conn_max_age=600, ssl_require=ssl_require)
    target_config = deepcopy(source_config)
    target_config.update(parsed)
    target_config.setdefault("TIME_ZONE", settings.TIME_ZONE if settings.USE_TZ else None)
    settings.DATABASES[TARGET_DB] = target_config
    connections.databases[TARGET_DB] = target_config
    connections[TARGET_DB].close()


def prompt_selection(candidates: list[dict], interactive: bool) -> list[dict]:
    if not candidates:
        return []
    if not interactive:
        return candidates
    if not os.isatty(0):
        return candidates

    self_lines = ["", "[PRUNE-INTERACTIVE] Delete candidates:"]
    for index, candidate in enumerate(candidates, start=1):
        self_lines.append(
            f"  {index}. {candidate['title'] or candidate['id']} "
            f"id={candidate['id']} reason={candidate['reason']} "
            f"documents={candidate.get('documents_count', 'n/a')} "
            f"chunks={candidate.get('chunks_count', 'n/a')} "
            f"analyses={candidate.get('analyses_count', 'n/a')}"
        )
    self_lines.append("Choose: comma-separated numbers, 'all', or 'skip'.")
    print("\n".join(self_lines), flush=True)

    while True:
        answer = input("Deletion selection: ").strip().lower()
        if answer in {"all", "a"}:
            return candidates
        if answer in {"skip", "s", ""}:
            return []
        try:
            indices = [int(part.strip()) for part in answer.split(",") if part.strip()]
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


def deal_rank_key(row: dict):
    return (
        str(row.get("created_at") or ""),
        str(row.get("id") or ""),
    )


def build_prune_candidates(local_deals: Iterable[Deal]) -> list[dict]:
    local_deals = list(local_deals)
    local_ids = {str(deal.id) for deal in local_deals}
    local_titles = {
        normalize_text(deal.title).lower()
        for deal in local_deals
        if normalize_text(deal.title)
    }

    prod_rows = (
        Deal.objects.using(TARGET_DB)
        .all()
        .values(
            "id",
            "title",
            "bank_id",
            "fund",
            "deal_status",
            "current_phase",
            "created_at",
        )
        .iterator(chunk_size=2000)
    )

    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in prod_rows:
        grouped[normalize_text(row["title"]).lower()].append(row)

    candidates: list[dict] = []
    for title_key, rows in grouped.items():
        exact_id_matches = [row for row in rows if str(row["id"]) in local_ids]
        if exact_id_matches:
            keep_row = sorted(exact_id_matches, key=deal_rank_key)[0]
            for row in rows:
                if row["id"] == keep_row["id"]:
                    continue
                candidates.append(
                    {
                        **row,
                        "reason": "duplicate-title",
                        "keep_id": keep_row["id"],
                    }
                )
            continue

        if title_key in local_titles:
            keep_row = sorted(rows, key=deal_rank_key)[0]
            for row in rows:
                if row["id"] == keep_row["id"]:
                    continue
                candidates.append(
                    {
                        **row,
                        "reason": "duplicate-title",
                        "keep_id": keep_row["id"],
                    }
                )
            continue

        for row in rows:
            candidates.append({**row, "reason": "orphan", "keep_id": None})

    candidates.sort(
        key=lambda row: (
            row["reason"],
            normalize_text(row.get("title")).lower(),
            str(row.get("id")),
        )
    )
    return candidates


def delete_batches(candidates: list[dict], batch_size: int = 250) -> None:
    batch_size = max(1, int(batch_size))
    for start in range(0, len(candidates), batch_size):
        batch = candidates[start : start + batch_size]
        batch_ids = [row["id"] for row in batch]
        preview = ", ".join((row["title"] or str(row["id"])) for row in batch[:3])
        if len(batch) > 3:
            preview += f", ... (+{len(batch) - 3} more)"
        print(
            f"[PRUNE-BATCH] deleting {len(batch)} deals ({start + 1}-{start + len(batch)} of {len(candidates)}): {preview}",
            flush=True,
        )
        with transaction.atomic(using=TARGET_DB):
            deleted_count, deleted_by_model = Deal.objects.using(TARGET_DB).filter(id__in=batch_ids).delete()
        print(
            f"[PRUNE-BATCH] deleted_count={deleted_count} model_breakdown={deleted_by_model}",
            flush=True,
        )


class Command(BaseCommand):
    help = "Interactively prune production Deal rows that are absent from the current local DB snapshot."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually delete the selected production deals. Default is dry-run.",
        )
        parser.add_argument(
            "--interactive",
            action="store_true",
            help="Prompt to select which prune candidates to delete.",
        )
        parser.add_argument(
            "--prod-database-url",
            default=os.environ.get("PROD_DATABASE_URL"),
            help="Production database URL. Defaults to PROD_DATABASE_URL.",
        )
        parser.add_argument(
            "--railway-cli",
            action="store_true",
            help="Read the production database URL from `railway variables --json` if not provided directly.",
        )
        parser.add_argument(
            "--railway-project-dir",
            default=".",
            help="Directory where Railway CLI should run.",
        )
        parser.add_argument(
            "--prod-db-ssl-require",
            action=argparse.BooleanOptionalAction,
            default=None,
            help="Whether the production DB connection should require SSL.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=250,
            help="Delete production prune candidates in batches of this size.",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Print the local/prod snapshot counts and every candidate.",
        )

    def handle(self, *args, **options):
        prod_database_url = options["prod_database_url"]
        if not prod_database_url and options["railway_cli"]:
            prod_database_url = database_url_from_railway_cli(options["railway_project_dir"])
        configure_target_database(prod_database_url, ssl_require=resolve_ssl_require(options["prod_db_ssl_require"]))

        local_deals = list(Deal.objects.using(SOURCE_DB).all().order_by("title", "id"))
        if not local_deals:
            self.stdout.write(self.style.WARNING("No local deals found. Nothing to prune."))
            return

        candidates = build_prune_candidates(local_deals)
        mode = "APPLY" if options["apply"] else "DRY-RUN"
        self.stdout.write(f">>> PRUNE ORPHAN PRODUCTION DEALS ({mode})")
        self.stdout.write(f"Local deals: {len(local_deals)}")
        self.stdout.write(f"Production prune candidates: {len(candidates)}")

        if options["verbose"]:
            self.stdout.write(
                f"Local titles={len({normalize_text(deal.title).lower() for deal in local_deals if normalize_text(deal.title)})} "
                f"local_ids={len({str(deal.id) for deal in local_deals})}"
            )

        selected = prompt_selection(candidates, interactive=options["interactive"])
        if not selected:
            self.stdout.write(self.style.WARNING("No production deals selected for deletion."))
            return

        self.stdout.write(f"Selected for deletion: {len(selected)}")
        for row in selected:
            self.stdout.write(
                f"[PRUNE{'-DRY-RUN' if not options['apply'] else ''}] {row['title']} "
                f"id={row['id']} reason={row['reason']} keep_id={row['keep_id'] or 'N/A'} "
                f"documents={row.get('documents_count', 'n/a')} "
                f"chunks={row.get('chunks_count', 'n/a')} "
                f"analyses={row.get('analyses_count', 'n/a')}"
            )

        if not options["apply"]:
            self.stdout.write(self.style.WARNING("Dry run only. No changes written."))
            return

        delete_batches(selected, batch_size=options["batch_size"])
        self.stdout.write(self.style.SUCCESS("Prune complete."))
