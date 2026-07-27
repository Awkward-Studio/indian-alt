from __future__ import annotations

import json
import re
import unicodedata
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from openpyxl import load_workbook

from deals.models import Deal


DEFAULT_WORKBOOKS = (
    ("1. Fund I.xlsx", "FUND1"),
    ("2. Fund II.xlsx", "FUND2"),
)
MISSING_VALUES = {"", "-", "nan", "none", "n/a", "na"}


def clean(value) -> str:
    text = str(value or "").strip()
    return "" if text.lower() in MISSING_VALUES else text


def normalized_title(value) -> str:
    text = unicodedata.normalize("NFKD", clean(value)).casefold()
    return re.sub(r"[^a-z0-9]+", "", text)


def parse_receipt_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = clean(value)
    if not text:
        return None
    for pattern in ("%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


class Command(BaseCommand):
    help = (
        "Reconcile Fund I/II workbook receipt dates and missing pass reasons. "
        "Dry-run by default; exact unique fund/title matches only."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--source-dir",
            default=".",
            help="Directory containing 1. Fund I.xlsx and 2. Fund II.xlsx.",
        )
        parser.add_argument("--apply", action="store_true", help="Persist safe backfills.")

    def handle(self, *args, **options):
        source_dir = Path(options["source_dir"]).resolve()
        workbook_specs = [(source_dir / name, fund) for name, fund in DEFAULT_WORKBOOKS]
        missing = [str(path) for path, _ in workbook_specs if not path.is_file()]
        if missing:
            raise CommandError(f"Workbook(s) not found: {', '.join(missing)}")

        workbook_rows = defaultdict(list)
        stats = defaultdict(int)
        examples = defaultdict(list)

        for path, expected_fund in workbook_specs:
            workbook = load_workbook(path, read_only=True, data_only=True)
            sheet = workbook.active
            rows = sheet.iter_rows(values_only=True)
            headers = [clean(value) for value in next(rows)]
            required = {"Deal Name", "Date of Receipt", "Reasons for Passing", "Fund"}
            missing_headers = required.difference(headers)
            if missing_headers:
                raise CommandError(f"{path.name} is missing columns: {sorted(missing_headers)}")
            positions = {name: headers.index(name) for name in required}

            for row_number, row in enumerate(rows, start=2):
                stats["workbook_rows"] += 1
                fund = clean(row[positions["Fund"]]).upper()
                title = clean(row[positions["Deal Name"]])
                key = (fund, normalized_title(title))
                if fund != expected_fund or not key[1]:
                    stats["skipped_invalid_workbook_rows"] += 1
                    continue
                workbook_rows[key].append(
                    {
                        "workbook": path.name,
                        "row": row_number,
                        "title": title,
                        "received_at": parse_receipt_date(row[positions["Date of Receipt"]]),
                        "reason": clean(row[positions["Reasons for Passing"]]),
                    }
                )

        database_deals = defaultdict(list)
        for deal in Deal.objects.filter(fund__in=["FUND1", "FUND2"]).only(
            "id", "title", "fund", "received_at", "reasons_for_passing",
            "rejection_reason", "deal_status", "current_phase",
        ):
            database_deals[(clean(deal.fund).upper(), normalized_title(deal.title))].append(deal)
            stats["database_deals"] += 1

        pending = []
        all_keys = set(workbook_rows) | set(database_deals)
        for key in all_keys:
            source_rows = workbook_rows.get(key, [])
            deals = database_deals.get(key, [])
            if len(source_rows) != 1 or len(deals) != 1:
                if not source_rows:
                    stats["database_without_workbook_match"] += len(deals)
                elif not deals:
                    stats["workbook_without_database_match"] += len(source_rows)
                else:
                    stats["ambiguous_keys"] += 1
                    if len(examples["ambiguous"]) < 10:
                        examples["ambiguous"].append({
                            "fund": key[0],
                            "titles": [item["title"] for item in source_rows],
                            "database_ids": [str(deal.id) for deal in deals],
                        })
                continue

            source = source_rows[0]
            deal = deals[0]
            stats["unique_matches"] += 1
            changes = {}

            source_date = source["received_at"]
            if source_date and deal.received_at is None:
                changes["received_at"] = source_date
                stats["dates_to_backfill"] += 1
            elif source_date and deal.received_at != source_date:
                stats["date_conflicts_preserved"] += 1
                if len(examples["date_conflicts"]) < 10:
                    examples["date_conflicts"].append({
                        "deal_id": str(deal.id),
                        "title": deal.title,
                        "database": deal.received_at.isoformat(),
                        "workbook": source_date.isoformat(),
                    })
            elif not source_date:
                stats["missing_workbook_dates"] += 1

            is_passed = "Passed" in {deal.deal_status, deal.current_phase}
            existing_reason = clean(deal.reasons_for_passing) or clean(deal.rejection_reason)
            if is_passed and source["reason"] and not existing_reason:
                changes["reasons_for_passing"] = source["reason"]
                changes["rejection_reason"] = source["reason"]
                stats["reasons_to_backfill"] += 1
            elif is_passed and not source["reason"] and not existing_reason:
                stats["passed_without_any_reason"] += 1

            if changes:
                pending.append((deal, changes))

        if options["apply"]:
            with transaction.atomic():
                for deal, changes in pending:
                    for field, value in changes.items():
                        setattr(deal, field, value)
                    deal.save(update_fields=list(changes))
                    stats["deals_updated"] += 1

        report = {
            "mode": "apply" if options["apply"] else "dry-run",
            "source_dir": str(source_dir),
            "stats": dict(sorted(stats.items())),
            "examples": dict(examples),
        }
        self.stdout.write(json.dumps(report, indent=2, default=str))
