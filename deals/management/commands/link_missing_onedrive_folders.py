"""Link missing Deal OneDrive folders by matching folder and deal names.

The command is deliberately dry-run by default. Use ``--apply`` only after
reviewing the proposed matches.
"""

from __future__ import annotations

import json
from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from deals.models import Deal
from deals.services.onedrive_folder_matching import FolderDealMatcher
from microsoft.services.graph_service import DMS_USER_EMAIL, GraphAPIService


class Command(BaseCommand):
    help = "Safely match missing Deal OneDrive links from the deal-folder root"

    def add_arguments(self, parser):
        parser.add_argument(
            "--email",
            default=DMS_USER_EMAIL,
            help=f"Microsoft account used to browse the folders (default: {DMS_USER_EMAIL})",
        )
        parser.add_argument(
            "--min-score",
            type=float,
            default=0.90,
            help="Minimum match score to consider (default: 0.90)",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Only inspect this many OneDrive folders (0 means all)",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persist safe matches; without this flag the command is a dry run",
        )
        parser.add_argument(
            "--interactive",
            action="store_true",
            help="Prompt before applying each safe match",
        )
        parser.add_argument(
            "--smart-interactive",
            action="store_true",
            help=(
                "Automatically apply exact/compact matches and prompt for fuzzy or ambiguous matches"
            ),
        )
        parser.add_argument(
            "--tui",
            action="store_true",
            help=(
                "Open a paginated multi-select terminal UI; select several links per page "
                "and use ROW:ALTERNATIVE for ambiguous matches"
            ),
        )
        parser.add_argument(
            "--tui-page-size",
            type=int,
            default=20,
            help="Number of matching rows shown per TUI page (default: 20)",
        )
        parser.add_argument(
            "--certain-score",
            type=float,
            default=0.98,
            help="Score threshold for automatic smart-mode linking (default: 0.98)",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            dest="output_json",
            help="Output the detailed matching report as JSON",
        )

    def handle(self, *args, **options):
        min_score = options["min_score"]
        if not 0 < min_score <= 1:
            raise CommandError("--min-score must be greater than 0 and at most 1")
        if options["tui_page_size"] < 1:
            raise CommandError("--tui-page-size must be at least 1")
        if options["tui"] and (options["interactive"] or options["smart_interactive"]):
            raise CommandError("Use --tui with optional --apply, not with --interactive or --smart-interactive")
        if options["apply"] and (options["interactive"] or options["smart_interactive"]):
            raise CommandError("Use only one of --apply, --interactive, or --smart-interactive")
        selected_modes = sum(
            bool(options[name]) for name in ("interactive", "smart_interactive", "tui")
        )
        if selected_modes > 1:
            raise CommandError("Use only one of --apply, --interactive, --smart-interactive, or --tui")
        if not 0 < options["certain_score"] <= 1:
            raise CommandError("--certain-score must be greater than 0 and at most 1")
        if options["certain_score"] < min_score:
            raise CommandError("--certain-score cannot be lower than --min-score")

        try:
            graph = GraphAPIService()
            folder_data = graph.get_deal_folder_root_children(options["email"])
        except Exception as exc:
            raise CommandError(f"Unable to read the OneDrive deal-folder root: {exc}") from exc

        folders = [
            item
            for item in folder_data.get("value", [])
            if item.get("id") and item.get("name") and item.get("folder") is not None
        ]
        if options["limit"]:
            folders = folders[: options["limit"]]

        missing_filter = Q(source_onedrive_id__isnull=True) | Q(source_onedrive_id="")
        missing_deals = list(Deal.objects.filter(missing_filter).order_by("title", "id"))
        all_deals = list(Deal.objects.all().only("id", "title", "source_onedrive_id", "source_drive_id"))
        linked_folder_deals = {
            deal.source_onedrive_id: deal
            for deal in all_deals
            if deal.source_onedrive_id
        }

        report = self._build_report(
            folders=folders,
            missing_deals=missing_deals,
            all_deals=all_deals,
            linked_folder_deals=linked_folder_deals,
            min_score=min_score,
        )

        if options["smart_interactive"]:
            self._smart_interactive_apply(report, certain_score=options["certain_score"])
        elif options["tui"]:
            self._tui_apply(report, page_size=options["tui_page_size"])
        elif options["interactive"]:
            self._interactive_apply(report)
        elif options["apply"]:
            self._apply_safe_matches(report)

        self._write_report(
            report,
            output_json=options["output_json"],
            apply=options["apply"] or options["interactive"] or options["smart_interactive"] or options["tui"],
            interactive=options["interactive"] or options["smart_interactive"] or options["tui"],
        )

    def _tui_apply(self, report, *, page_size):
        """Apply several reviewed links per page without adding a dependency."""
        candidates = [
            record for record in report
            if record["status"] in {"ready", "ambiguous"}
        ]
        if not candidates:
            self.stdout.write(
                "\nTUI: no actionable matches were found. "
                "Only 'ready' and 'ambiguous' rows appear here; inspect the summary "
                "for no_match, already_linked_to_deal, and duplicate-folder rows."
            )
            return
        page = 0

        while candidates:
            total_pages = (len(candidates) + page_size - 1) // page_size
            page = max(0, min(page, total_pages - 1))
            start = page * page_size
            page_records = candidates[start:start + page_size]

            self.stdout.write(
                f"\nLinking TUI · page {page + 1}/{total_pages} "
                f"· rows {start + 1}-{start + len(page_records)} of {len(candidates)}"
            )
            for index, record in enumerate(page_records, start=1):
                status = record["status"]
                suffix = ""
                if status == "ambiguous":
                    alternatives = record.get("alternatives", [])
                    suffix = " · choices: " + "; ".join(
                        f"{choice_index}:{alternative['deal_title']}"
                        for choice_index, alternative in enumerate(alternatives, start=1)
                    )
                self.stdout.write(
                    f"  [{index:>2}] {record['folder_name']} -> "
                    f"{record['deal_title']} ({record['score']:.2f}, {status}){suffix}"
                )

            self.stdout.write(
                "Select rows (e.g. 1,3-5; ambiguous: 2:1), "
                "a=all ready, n=next, p=previous, q=quit: ",
                ending="",
            )
            try:
                answer = input().strip().lower()
            except EOFError:
                answer = "q"

            if answer in {"q", "quit"}:
                break
            if answer in {"n", "next"}:
                if page < total_pages - 1:
                    page += 1
                else:
                    self.stdout.write("Already on the last page.")
                continue
            if answer in {"p", "previous", "prev"}:
                if page > 0:
                    page -= 1
                else:
                    self.stdout.write("Already on the first page.")
                continue
            if answer in {"a", "all"}:
                selections = [(index, None) for index, record in enumerate(page_records, start=1) if record["status"] == "ready"]
            else:
                selections = self._parse_tui_selections(answer, len(page_records))

            if selections is None:
                self.stdout.write("Invalid selection. Use row numbers, ranges, or ROW:ALTERNATIVE.")
                continue
            if not selections:
                self.stdout.write("No rows selected.")
                continue

            selected_records = []
            invalid_selection = False
            for row_number, alternative_number in selections:
                record = page_records[row_number - 1]
                if record["status"] == "ready":
                    if alternative_number is not None:
                        invalid_selection = True
                        self.stdout.write(f"Row {row_number} is not ambiguous; select it without :ALTERNATIVE.")
                        break
                    selected_records.append(record)
                    continue

                alternatives = record.get("alternatives", [])
                if alternative_number is None:
                    invalid_selection = True
                    self.stdout.write(f"Row {row_number} is ambiguous; choose it as {row_number}:1, {row_number}:2, etc.")
                    break
                if not 1 <= alternative_number <= len(alternatives):
                    invalid_selection = True
                    self.stdout.write(f"Row {row_number} has {len(alternatives)} alternatives.")
                    break
                selected = alternatives[alternative_number - 1]
                record.update(
                    deal_id=selected["deal_id"],
                    deal_title=selected["deal_title"],
                    score=selected["score"],
                    reason=selected.get("reason", "selected from ambiguous candidates"),
                    status="ready",
                )
                selected_records.append(record)

            if invalid_selection:
                continue
            self._apply_safe_matches(selected_records)
            candidates = [
                record for record in candidates
                if record["status"] in {"ready", "ambiguous"}
            ]
            if page >= (len(candidates) + page_size - 1) // page_size and page > 0:
                page -= 1

    @staticmethod
    def _parse_tui_selections(answer, page_size):
        if not answer:
            return []
        selections = []
        seen = set()
        for raw_token in answer.replace(" ", "").split(","):
            if not raw_token:
                return None
            if ":" in raw_token:
                row_token, alternative_token = raw_token.split(":", 1)
                try:
                    row = int(row_token)
                    alternative = int(alternative_token)
                except ValueError:
                    return None
                values = [(row, alternative)]
            elif "-" in raw_token:
                try:
                    first, last = (int(value) for value in raw_token.split("-", 1))
                except ValueError:
                    return None
                if first > last:
                    return None
                values = [(row, None) for row in range(first, last + 1)]
            else:
                try:
                    values = [(int(raw_token), None)]
                except ValueError:
                    return None
            for selection in values:
                row = selection[0]
                if not 1 <= row <= page_size or selection in seen:
                    if not 1 <= row <= page_size:
                        return None
                    continue
                seen.add(selection)
                selections.append(selection)
        return selections

    def _build_report(self, *, folders, missing_deals, all_deals, linked_folder_deals, min_score):
        records = []
        proposed_by_deal = defaultdict(list)
        matcher = FolderDealMatcher(all_deals)

        for folder in folders:
            folder_id = folder["id"]
            folder_name = folder["name"]
            ranked = [
                (match, deal)
                for match, deal in matcher.rank(folder_name)
                if match.score >= min_score
            ]

            record = {
                "folder_id": folder_id,
                "folder_name": folder_name,
                "drive_id": folder.get("driveId"),
                "deal_id": None,
                "deal_title": None,
                "score": None,
                "reason": None,
                "status": "no_match",
            }
            if not ranked:
                records.append(record)
                continue

            best_match, best_deal = ranked[0]
            record.update(
                deal_id=str(best_deal.id),
                deal_title=best_deal.title,
                score=best_match.score,
                reason=best_match.reason,
            )

            if best_deal.source_onedrive_id:
                record["status"] = "already_linked_to_deal"
            elif len(ranked) > 1 and ranked[1][0].score >= best_match.score - 0.08:
                record["status"] = "ambiguous"
                record["alternatives"] = [
                    {
                        "deal_id": str(deal.id),
                        "deal_title": deal.title,
                        "score": match.score,
                        "reason": match.reason,
                    }
                    for match, deal in ranked[:5]
                ]
            else:
                record["status"] = "ready"
                proposed_by_deal[str(best_deal.id)].append(record)

            records.append(record)

        for records_for_deal in proposed_by_deal.values():
            if len(records_for_deal) <= 1:
                continue
            for record in records_for_deal:
                record["status"] = "ambiguous_duplicate_folder_match"
                record["alternatives"] = [
                    {"folder_id": other["folder_id"], "folder_name": other["folder_name"]}
                    for other in records_for_deal
                    if other is not record
                ]

        for record in records:
            existing_deal = linked_folder_deals.get(record["folder_id"])
            if existing_deal and record["status"] == "ready":
                record["status"] = "folder_already_linked"
                record["linked_deal_id"] = str(existing_deal.id)
                record["linked_deal_title"] = existing_deal.title

        return records

    def _interactive_apply(self, report):
        for record in report:
            if record["status"] != "ready":
                continue

            self.stdout.write(
                "\nProposed link:\n"
                f"  OneDrive folder: {record['folder_name']}\n"
                f"  Folder ID:       {record['folder_id']}\n"
                f"  Deal:             {record['deal_title']}\n"
                f"  Deal ID:          {record['deal_id']}\n"
                f"  Match:            {record['score']:.2f} ({record['reason']})"
            )
            self.stdout.write("Link this folder to this deal? [y]es / [n]o / [q]uit: ", ending="")
            try:
                answer = input().strip().lower()
            except EOFError:
                answer = "q"
            if answer in {"q", "quit"}:
                break
            if answer not in {"y", "yes"}:
                record["status"] = "declined"
                continue

            self._apply_safe_matches([record])

    def _smart_interactive_apply(self, report, *, certain_score):
        for record in report:
            if record["status"] == "ready" and record["score"] >= certain_score:
                self._apply_safe_matches([record])
                continue

            if record["status"] == "ready":
                self.stdout.write(
                    "\nProposed fuzzy link:\n"
                    f"  OneDrive folder: {record['folder_name']}\n"
                    f"  Deal:             {record['deal_title']}\n"
                    f"  Match:            {record['score']:.2f} ({record['reason']})"
                )
                self.stdout.write("Accept this link? [y]es / [n]o / [q]uit: ", ending="")
                try:
                    answer = input().strip().lower()
                except EOFError:
                    answer = "q"
                if answer in {"q", "quit"}:
                    break
                if answer in {"y", "yes"}:
                    self._apply_safe_matches([record])
                else:
                    record["status"] = "declined"
                continue

            if record["status"] != "ambiguous":
                continue

            alternatives = record.get("alternatives", [])
            self.stdout.write(
                "\nAmbiguous folder match:\n"
                f"  OneDrive folder: {record['folder_name']}\n"
            )
            for index, alternative in enumerate(alternatives, start=1):
                self.stdout.write(
                    f"  {index}. {alternative['deal_title']} "
                    f"({alternative['score']:.2f}, {alternative.get('reason', 'match')})"
                )
            self.stdout.write("Choose a deal number, [s]kip, or [q]uit: ", ending="")
            try:
                answer = input().strip().lower()
            except EOFError:
                answer = "q"
            if answer in {"q", "quit"}:
                break
            if answer in {"s", "skip", "n", "no"}:
                record["status"] = "declined"
                continue
            try:
                selected = alternatives[int(answer) - 1]
            except (ValueError, IndexError):
                record["status"] = "declined"
                continue

            record.update(
                deal_id=selected["deal_id"],
                deal_title=selected["deal_title"],
                score=selected["score"],
                reason=selected.get("reason", "selected from ambiguous candidates"),
                status="ready",
            )
            self._apply_safe_matches([record])

    @staticmethod
    def _apply_safe_matches(report):
        with transaction.atomic():
            for record in report:
                if record["status"] != "ready":
                    continue

                deal = Deal.objects.select_for_update().get(pk=record["deal_id"])
                if deal.source_onedrive_id:
                    record["status"] = "skipped_deal_linked_during_apply"
                    continue

                folder_is_taken = Deal.objects.filter(
                    source_onedrive_id=record["folder_id"]
                ).exclude(pk=deal.pk).exists()
                if folder_is_taken:
                    record["status"] = "skipped_folder_linked_during_apply"
                    continue

                deal.source_onedrive_id = record["folder_id"]
                deal.source_drive_id = record["drive_id"] or ""
                deal.save(update_fields=["source_onedrive_id", "source_drive_id", "updated_at"])
                record["status"] = "linked"

    def _write_report(self, report, *, output_json, apply, interactive=False):
        counts = defaultdict(int)
        for record in report:
            counts[record["status"]] += 1

        if output_json:
            self.stdout.write(json.dumps({"summary": dict(counts), "folders": report}, indent=2))
            return

        mode = "INTERACTIVE" if interactive else ("APPLY" if apply else "DRY RUN")
        self.stdout.write(f"OneDrive folder linking ({mode})")
        self.stdout.write(f"Inspected {len(report)} folder(s).")
        for status in sorted(counts):
            self.stdout.write(f"  {status}: {counts[status]}")

        actionable = {"ready", "linked", "skipped_deal_linked_during_apply", "skipped_folder_linked_during_apply"}
        rows = [record for record in report if record["status"] in actionable]
        if rows:
            self.stdout.write("\nCandidate links:")
            for record in rows:
                self.stdout.write(
                    f"  [{record['status']}] {record['folder_name']} -> "
                    f"{record['deal_title']} ({record['score']:.2f}, {record['reason']})"
                )
        if not apply and counts.get("ready"):
            self.stdout.write("\nNo database changes made. Re-run with --apply after reviewing the report.")
