#!/usr/bin/env python
import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from import_deals_from_excel import (
    Deal,
    build_payload,
    load_rows,
    normalize_text,
    write_report,
)


@dataclass
class PurgeRowResult:
    row_number: int
    deal_name: str
    status: str
    message: str
    matched_ids: list[str]
    matched_titles: list[str]
    deleted_ids: list[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Preview or purge deals previously imported from an Excel workbook."
    )
    parser.add_argument("excel_path", help="Path to the original .xlsx file used for import.")
    parser.add_argument("--sheet", help="Worksheet name to inspect. Defaults to the active sheet.")
    parser.add_argument(
        "--confirm-delete",
        action="store_true",
        help="Actually delete the uniquely matched deals. Default is preview only.",
    )
    parser.add_argument("--report", help="Optional path to write a JSON preview/deletion report.")
    return parser.parse_args()


def same_date(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return left.date() == right.date()


def resolve_candidates(payload: dict[str, Any]) -> list[Deal]:
    created_at = payload.get("_created_at")
    base_queryset = Deal.objects.filter(title__iexact=payload["title"]).order_by("-created_at")
    candidates = list(base_queryset)

    def narrow(current: list[Deal], predicate) -> list[Deal]:
        filtered = [deal for deal in current if predicate(deal)]
        return filtered if filtered else current

    candidates = narrow(candidates, lambda deal: normalize_text(deal.fund) == normalize_text(payload["fund"]))
    candidates = narrow(
        candidates,
        lambda deal: normalize_text(deal.legacy_investment_bank) == normalize_text(payload["legacy_investment_bank"]),
    )
    candidates = narrow(candidates, lambda deal: normalize_text(deal.funding_ask) == normalize_text(payload["funding_ask"]))
    candidates = narrow(candidates, lambda deal: normalize_text(deal.deal_status) == normalize_text(payload["deal_status"]))
    candidates = narrow(candidates, lambda deal: normalize_text(deal.city) == normalize_text(payload["city"]))
    candidates = narrow(candidates, lambda deal: normalize_text(deal.industry) == normalize_text(payload["industry"]))
    candidates = narrow(candidates, lambda deal: normalize_text(deal.sector) == normalize_text(payload["sector"]))
    candidates = narrow(candidates, lambda deal: bool(deal.is_female_led) is bool(payload["is_female_led"]))
    candidates = narrow(candidates, lambda deal: bool(deal.management_meeting) is bool(payload["management_meeting"]))
    candidates = narrow(
        candidates,
        lambda deal: bool(deal.business_proposal_stage) is bool(payload["business_proposal_stage"]),
    )
    candidates = narrow(candidates, lambda deal: bool(deal.ic_stage) is bool(payload["ic_stage"]))

    if created_at is not None:
        candidates = narrow(candidates, lambda deal: same_date(deal.created_at, created_at))

    exact_matches = [
        deal
        for deal in candidates
        if normalize_text(deal.fund) == normalize_text(payload["fund"])
        and normalize_text(deal.legacy_investment_bank) == normalize_text(payload["legacy_investment_bank"])
        and normalize_text(deal.funding_ask) == normalize_text(payload["funding_ask"])
        and normalize_text(deal.deal_status) == normalize_text(payload["deal_status"])
        and normalize_text(deal.city) == normalize_text(payload["city"])
        and normalize_text(deal.industry) == normalize_text(payload["industry"])
        and normalize_text(deal.sector) == normalize_text(payload["sector"])
        and bool(deal.is_female_led) is bool(payload["is_female_led"])
        and bool(deal.management_meeting) is bool(payload["management_meeting"])
        and bool(deal.business_proposal_stage) is bool(payload["business_proposal_stage"])
        and bool(deal.ic_stage) is bool(payload["ic_stage"])
        and (created_at is None or same_date(deal.created_at, created_at))
    ]
    if exact_matches:
        return exact_matches

    return candidates


def build_row_results(rows: list[tuple[int, dict[str, Any]]], confirm_delete: bool) -> list[PurgeRowResult]:
    results: list[PurgeRowResult] = []

    for row_number, row in rows:
        deal_name = normalize_text(row.get("Deal Name"))
        if not deal_name:
            continue

        try:
            payload = build_payload(row)
            matches = resolve_candidates(payload)

            if not matches:
                results.append(
                    PurgeRowResult(
                        row_number=row_number,
                        deal_name=deal_name,
                        status="missing",
                        message="No current deal matched this workbook row.",
                        matched_ids=[],
                        matched_titles=[],
                        deleted_ids=[],
                    )
                )
                continue

            if len(matches) > 1:
                results.append(
                    PurgeRowResult(
                        row_number=row_number,
                        deal_name=deal_name,
                        status="ambiguous",
                        message="Multiple current deals matched this workbook row.",
                        matched_ids=[str(deal.id) for deal in matches],
                        matched_titles=[deal.title or str(deal.id) for deal in matches],
                        deleted_ids=[],
                    )
                )
                continue

            match = matches[0]
            deleted_ids: list[str] = []
            status = "matched"
            message = "Matched current deal."

            if confirm_delete:
                deleted_ids = [str(match.id)]
                match.delete()
                status = "deleted"
                message = "Deleted matched deal."

            results.append(
                PurgeRowResult(
                    row_number=row_number,
                    deal_name=deal_name,
                    status=status,
                    message=message,
                    matched_ids=[str(match.id)],
                    matched_titles=[match.title or str(match.id)],
                    deleted_ids=deleted_ids,
                )
            )
        except Exception as exc:  # noqa: BLE001
            results.append(
                PurgeRowResult(
                    row_number=row_number,
                    deal_name=deal_name,
                    status="failed",
                    message=str(exc),
                    matched_ids=[],
                    matched_titles=[],
                    deleted_ids=[],
                )
            )

    return results


def summarize_results(results: list[PurgeRowResult], confirm_delete: bool) -> dict[str, Any]:
    return {
        "total_rows": len(results),
        "matched": sum(1 for result in results if result.status == "matched"),
        "deleted": sum(1 for result in results if result.status == "deleted"),
        "missing": sum(1 for result in results if result.status == "missing"),
        "ambiguous": sum(1 for result in results if result.status == "ambiguous"),
        "failed": sum(1 for result in results if result.status == "failed"),
        "mode": "delete" if confirm_delete else "preview",
        "rows": [
            {
                "row_number": result.row_number,
                "deal_name": result.deal_name,
                "status": result.status,
                "message": result.message,
                "matched_ids": result.matched_ids,
                "matched_titles": result.matched_titles,
                "deleted_ids": result.deleted_ids,
            }
            for result in results
        ],
    }


def print_summary(summary: dict[str, Any]) -> None:
    print("")
    print("Purge summary")
    print(f"  Mode:       {summary['mode']}")
    print(f"  Total rows: {summary['total_rows']}")
    print(f"  Matched:    {summary['matched']}")
    print(f"  Deleted:    {summary['deleted']}")
    print(f"  Missing:    {summary['missing']}")
    print(f"  Ambiguous:  {summary['ambiguous']}")
    print(f"  Failed:     {summary['failed']}")

    if summary["rows"]:
      print("")
      print("Row details")
      for row in summary["rows"]:
          targets = ", ".join(row["matched_titles"]) if row["matched_titles"] else "-"
          print(
              f"  Row {row['row_number']}: {row['status'].upper()} - "
              f"{row['deal_name'] or '(blank deal name)'} - {row['message']} - matches: {targets}"
          )


def main() -> int:
    args = parse_args()
    excel_path = Path(args.excel_path).expanduser().resolve()

    if not excel_path.exists():
        print(f"Excel file not found: {excel_path}")
        return 1

    try:
        _, rows = load_rows(excel_path, args.sheet)
        results = build_row_results(rows, confirm_delete=False)
        ambiguous = [result for result in results if result.status == "ambiguous"]

        if args.confirm_delete and ambiguous:
            summary = summarize_results(results, confirm_delete=False)
            print_summary(summary)
            print("")
            print("Deletion refused: ambiguous workbook rows must be resolved before deleting.")
            if args.report:
                write_report(Path(args.report).expanduser().resolve(), summary)
            return 2

        if args.confirm_delete:
            results = build_row_results(rows, confirm_delete=True)

        summary = summarize_results(results, confirm_delete=args.confirm_delete)
        print_summary(summary)

        if args.report:
            write_report(Path(args.report).expanduser().resolve(), summary)
    except Exception as exc:  # noqa: BLE001
        print(f"Purge failed: {exc}")
        return 1

    return 0 if summary["failed"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
