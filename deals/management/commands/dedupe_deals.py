from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db.models import Count

from deals.models import Deal
from deals.services.deal_merge import merge_deal_into_canonical


def deal_rank_key(deal: Deal):
    return (
        -deal.documents.count(),
        -deal.chunks.count(),
        -deal.analyses.count(),
        deal.created_at.isoformat() if deal.created_at else "",
        str(deal.id),
    )


class Command(BaseCommand):
    help = "Merge duplicate Deal rows by title without changing the current schema."

    def add_arguments(self, parser):
        parser.add_argument("--title", action="append", help="Optional exact title to dedupe. Can be passed multiple times.")
        parser.add_argument("--dry-run", action="store_true", help="Show what would be merged without writing changes.")

    def handle(self, *args, **options):
        titles = [value.strip() for value in options["title"] or [] if value and value.strip()]
        dry_run = options["dry_run"]

        duplicate_groups = (
            Deal.objects.values("title")
            .annotate(count=Count("id"))
            .filter(count__gt=1)
            .order_by("title")
        )
        if titles:
            duplicate_groups = duplicate_groups.filter(title__in=titles)

        groups = list(duplicate_groups)
        if not groups:
            self.stdout.write(self.style.SUCCESS("No duplicate deal titles found for the requested scope."))
            return

        self.stdout.write(f"Found {len(groups)} duplicate title groups.")
        for group in groups:
            title = group["title"]
            deals = list(Deal.objects.filter(title=title).order_by("created_at", "id"))
            deals.sort(key=deal_rank_key)
            canonical = deals[0]
            duplicates = deals[1:]
            self.stdout.write(f"{title}: canonical={canonical.id} duplicates={len(duplicates)}")
            for duplicate in duplicates:
                self.stdout.write(f"  merge {duplicate.id} -> {canonical.id}")
                if dry_run:
                    continue
                merge_deal_into_canonical(canonical, duplicate)

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run only. No changes written."))
        else:
            self.stdout.write(self.style.SUCCESS("Deal dedupe complete."))
