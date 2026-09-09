"""Consolidate duplicate Deal rows around the already-linked canonical deal.

The command is dry-run by default. It only proposes a group when exactly one
deal in the normalized-title group has a OneDrive folder; groups with multiple
linked deals are left for manual review.
"""

from __future__ import annotations

from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count

from deals.models import Deal
from deals.services.deal_merge import merge_deal_into_canonical
from deals.services.onedrive_folder_matching import normalize_folder_deal_name


PLACEHOLDER_TITLES = {
    "",
    "-",
    "na",
    "n a",
    "n/a",
    "none",
    "not given",
    "not disclosed",
    "unknown",
}


def duplicate_group_key(title):
    normalized = normalize_folder_deal_name(title)
    return normalized if normalized not in PLACEHOLDER_TITLES else ""


class Command(BaseCommand):
    help = "Safely merge duplicate deals into the single duplicate already linked to OneDrive"

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Merge and delete safe duplicate rows; without this flag the command is a dry run",
        )
        parser.add_argument(
            "--interactive",
            action="store_true",
            help="Prompt before applying each safe duplicate group",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Only show or process this many safe groups (0 means all)",
        )

    def handle(self, *args, **options):
        if options["limit"] < 0:
            raise CommandError("--limit cannot be negative")
        if options["apply"] and options["interactive"]:
            raise CommandError("Use only one of --apply or --interactive")

        groups = self._find_safe_groups()
        if options["limit"]:
            groups = groups[: options["limit"]]

        mode = "INTERACTIVE" if options["interactive"] else "APPLY" if options["apply"] else "DRY RUN"
        self.stdout.write(f"Duplicate deal pruning ({mode})")
        self.stdout.write(f"Found {len(groups)} safe duplicate group(s).")

        for group in groups:
            self._write_group(group)

        if options["interactive"]:
            for group in groups:
                self.stdout.write("Merge this group into the linked deal? [y]es / [n]o / [q]uit: ", ending="")
                try:
                    answer = input().strip().lower()
                except EOFError:
                    answer = "q"
                if answer in {"q", "quit"}:
                    break
                if answer in {"y", "yes"}:
                    self._apply_group(group)
        elif options["apply"]:
            for group in groups:
                self._apply_group(group)
        elif groups:
            self.stdout.write("\nNo database changes made. Re-run with --apply after reviewing the groups.")

    def _find_safe_groups(self):
        grouped = defaultdict(list)
        deals = Deal.objects.annotate(
            document_count=Count("documents", distinct=True),
            analysis_count=Count("analyses", distinct=True),
        ).order_by("title", "id")
        for deal in deals:
            key = duplicate_group_key(deal.title)
            if key:
                grouped[key].append(deal)

        safe_groups = []
        for normalized_title, members in sorted(grouped.items()):
            linked = [deal for deal in members if (deal.source_onedrive_id or "").strip()]
            unlinked = [deal for deal in members if not (deal.source_onedrive_id or "").strip()]
            if len(linked) != 1 or not unlinked:
                continue
            safe_groups.append({
                "normalized_title": normalized_title,
                "canonical": linked[0],
                "duplicates": unlinked,
            })
        return safe_groups

    def _write_group(self, group):
        canonical = group["canonical"]
        self.stdout.write(
            f"\nKEEP linked: {canonical.title} [{canonical.id}] "
            f"· {canonical.document_count} document(s) · folder {canonical.source_onedrive_id}"
        )
        for duplicate in group["duplicates"]:
            self.stdout.write(
                f"  MERGE and remove: {duplicate.title} [{duplicate.id}] "
                f"· {duplicate.document_count} document(s) · {duplicate.analysis_count} analysis record(s)"
            )

    @staticmethod
    def _apply_group(group):
        canonical = Deal.objects.get(pk=group["canonical"].pk)
        for duplicate in group["duplicates"]:
            duplicate = Deal.objects.get(pk=duplicate.pk)
            if canonical.source_onedrive_id and not duplicate.source_onedrive_id:
                merge_deal_into_canonical(canonical, duplicate)
                canonical.refresh_from_db()
