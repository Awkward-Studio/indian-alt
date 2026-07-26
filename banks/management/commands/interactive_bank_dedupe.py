from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from banks.models import Bank
from deals.services.entity_dedupe import (
    bank_candidate_groups,
    bank_retention_recommendations,
    format_recommendation,
    merge_many_banks,
    summarize_bank,
)


def parse_indices(value: str, max_index: int) -> list[int]:
    selected: list[int] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, end = [int(item.strip()) for item in part.split("-", 1)]
            selected.extend(range(start, end + 1))
        else:
            selected.append(int(part))
    invalid = [idx for idx in selected if idx < 1 or idx > max_index]
    if invalid:
        raise ValueError(f"Index out of range: {invalid[0]}")
    deduped = []
    for idx in selected:
        if idx not in deduped:
            deduped.append(idx)
    return deduped


class Command(BaseCommand):
    help = "Interactively merge duplicate Bank rows while preserving deal and contact relationships."

    def add_arguments(self, parser):
        parser.add_argument("--match", choices=["normalized_name", "exact_name", "domain"], default="normalized_name")
        parser.add_argument("--key", help="Only review one candidate key from the chosen matcher.")
        parser.add_argument("--limit", type=int, default=0, help="Maximum candidate groups to review.")
        parser.add_argument("--apply", action="store_true", help="Write merges. Default is dry-run.")
        parser.add_argument("--yes", action="store_true", help="With --apply, skip final per-group confirmation.")
        parser.add_argument(
            "--auto-confidence",
            choices=["high", "medium"],
            help=(
                "Automatically merge groups whose recommended canonical has at least this confidence. "
                "Still dry-run unless --apply is passed."
            ),
        )

    def handle(self, *args, **options):
        groups = bank_candidate_groups(match=options["match"])
        if options.get("key"):
            groups = [group for group in groups if group.key == options["key"]]
        if options["limit"]:
            groups = groups[: options["limit"]]

        if not groups:
            self.stdout.write(self.style.SUCCESS("No duplicate bank candidate groups found."))
            return

        mode = "APPLY" if options["apply"] else "DRY-RUN"
        self.stdout.write(f"{mode}: reviewing {len(groups)} bank duplicate candidate group(s).")
        confidence_rank = {"duplicate": 0, "low": 1, "medium": 2, "high": 3}
        auto_threshold = confidence_rank.get(options.get("auto_confidence") or "", None)

        for group_index, group in enumerate(groups, 1):
            self.stdout.write("")
            self.stdout.write(self.style.MIGRATE_HEADING(f"[{group_index}/{len(groups)}] {group.match_type}: {group.key}"))
            recommendations = bank_retention_recommendations(group.items)
            best_score = max((rec.score for rec in recommendations.values()), default=None)
            for index, bank in enumerate(group.items, 1):
                recommendation = recommendations.get(str(bank.id))
                recommended = ""
                if recommendation and recommendation.score == best_score:
                    recommended = "  [recommended canonical]"
                self.stdout.write(f"  {index}. {summarize_bank(bank)}{recommended}")
                self.stdout.write(f"     {format_recommendation(recommendation)}")

            recommended_indices = [
                index
                for index, bank in enumerate(group.items, 1)
                if recommendations.get(str(bank.id)) and recommendations[str(bank.id)].score == best_score
            ]
            auto_merge = False
            if auto_threshold is not None and len(recommended_indices) == 1:
                recommended = recommendations.get(str(group.items[recommended_indices[0] - 1].id))
                auto_merge = confidence_rank.get(recommended.confidence, 0) >= auto_threshold

            if auto_merge:
                canonical_index = recommended_indices[0]
                self.stdout.write(f"  Auto-selected canonical index {canonical_index}.")
            elif auto_threshold is not None:
                self.stdout.write("  Skipped by auto-confidence threshold.")
                continue
            else:
                action = input("Choose canonical index, 's' to skip, or 'q' to quit [1]: ").strip().lower()
                if action == "q":
                    return
                if action == "s":
                    continue
                canonical_index = int(action or "1")
            if canonical_index < 1 or canonical_index > len(group.items):
                raise CommandError("Canonical index out of range.")

            canonical = group.items[canonical_index - 1]
            default_duplicates = [idx for idx in range(1, len(group.items) + 1) if idx != canonical_index]
            if auto_merge:
                duplicate_answer = ",".join(str(idx) for idx in default_duplicates)
                self.stdout.write(f"  Auto-selected duplicate indices {duplicate_answer}.")
            else:
                duplicate_answer = input(
                    f"Duplicate indices to merge into {canonical_index} "
                    f"[{','.join(str(idx) for idx in default_duplicates)}]: "
                ).strip()
            duplicate_indices = parse_indices(
                duplicate_answer or ",".join(str(idx) for idx in default_duplicates),
                len(group.items),
            )
            duplicates = [group.items[idx - 1] for idx in duplicate_indices if idx != canonical_index]
            if not duplicates:
                self.stdout.write("  No duplicates selected.")
                continue

            self.stdout.write(f"  Canonical: {summarize_bank(canonical)}")
            for duplicate in duplicates:
                self.stdout.write(f"  Merge:     {summarize_bank(duplicate)}")

            if not options["apply"]:
                self.stdout.write(self.style.WARNING("  Dry-run only. Re-run with --apply to write this merge."))
                continue
            if not options["yes"] and not auto_merge:
                confirm = input("Apply this bank merge? This deletes duplicate bank rows. [y/N]: ").strip().lower()
                if confirm != "y":
                    continue
            merge_many_banks(canonical, duplicates)
            self.stdout.write(self.style.SUCCESS("  Bank merge complete."))
