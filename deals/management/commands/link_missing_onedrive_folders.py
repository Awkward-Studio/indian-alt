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
            "--json",
            action="store_true",
            dest="output_json",
            help="Output the detailed matching report as JSON",
        )

    def handle(self, *args, **options):
        min_score = options["min_score"]
        if not 0 < min_score <= 1:
            raise CommandError("--min-score must be greater than 0 and at most 1")
        if options["apply"] and options["interactive"]:
            raise CommandError("Use either --apply or --interactive, not both")

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

        if options["interactive"]:
            self._interactive_apply(report)
        elif options["apply"]:
            self._apply_safe_matches(report)

        self._write_report(
            report,
            output_json=options["output_json"],
            apply=options["apply"] or options["interactive"],
            interactive=options["interactive"],
        )

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
                    {"deal_id": str(deal.id), "deal_title": deal.title, "score": match.score}
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
