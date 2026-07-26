from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count

from deals.models import Deal
from deals.services.deal_merge import merge_deal_into_canonical


ADVISOR_SUFFIX_RE = re.compile(
    r"\s+[-–—]\s+("
    r"intequant|o3|ey|deloitte|avendus|unitus|anand rathi|motilal oswal|jmfl|spark|"
    r"maple|palanca|beacon|masterkey|incred|indigoedge|prop|proprietary|aurum|veda|"
    r"fundtq|pwc|kpmg|dexter|equirus|ambit|steer|centrum|rbsa|yukon|right pillar|"
    r"merisis|intellecap|nuvama|systematix|blue seiner|wodehouse"
    r").*$",
    re.IGNORECASE,
)


@dataclass
class DealRecommendation:
    score: int
    confidence: str
    reasons: list[str]
    warnings: list[str]


def normalize_deal_title(title: str | None) -> str:
    text = (title or "").strip().lower()
    text = re.sub(r"^project\s+", "", text)
    text = ADVISOR_SUFFIX_RE.sub("", text)
    text = re.sub(r"\((project\s+)?([^)]+)\)", r" \2 ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def confidence_from_margin(best: int, next_best: int | None) -> str:
    if next_best is None:
        return "high"
    margin = best - next_best
    if margin >= 6:
        return "high"
    if margin >= 3:
        return "medium"
    return "low"


def source_id_set(deals: list[Deal]) -> set[str]:
    return {str(deal.source_onedrive_id).strip() for deal in deals if str(deal.source_onedrive_id or "").strip()}


def deal_score(deal: Deal) -> tuple[int, list[str]]:
    score = 0
    reasons = []

    document_count = deal.documents.count()
    chunk_count = deal.chunks.count()
    analysis_count = deal.analyses.count()
    if deal.source_onedrive_id:
        score += 10
        reasons.append("source-backed")
    if document_count:
        score += min(10, document_count * 2)
        reasons.append(f"{document_count} document(s)")
    if chunk_count:
        score += min(8, max(1, chunk_count // 10))
        reasons.append(f"{chunk_count} chunk(s)")
    if analysis_count:
        score += min(6, analysis_count)
        reasons.append(f"{analysis_count} analysis row(s)")
    if deal.bank_id:
        score += 2
        reasons.append("has bank")
    if deal.primary_contact_id:
        score += 2
        reasons.append("has primary contact")
    if deal.current_phase and deal.current_phase != "1: Deal Sourced":
        score += 1
        reasons.append(f"phase={deal.current_phase}")
    return score, reasons or ["no strong retention signal"]


def recommendations_for_group(deals: list[Deal]) -> dict[str, DealRecommendation]:
    raw = []
    source_ids = source_id_set(deals)
    same_source_group = len(source_ids) == 1 and all(deal.source_onedrive_id for deal in deals)
    distinct_source_group = len(source_ids) > 1
    for deal in deals:
        score, reasons = deal_score(deal)
        warnings = []
        if same_source_group:
            score += 20
            reasons.append("same OneDrive source folder as group")
        if distinct_source_group and deal.source_onedrive_id:
            warnings.append("multiple source folders in group")
        if deal.current_phase == "Portfolio":
            warnings.append("portfolio row")
        raw.append((deal, score, reasons, warnings))

    ordered_scores = sorted((score for _deal, score, _reasons, _warnings in raw), reverse=True)
    best = ordered_scores[0] if ordered_scores else 0
    next_best = ordered_scores[1] if len(ordered_scores) > 1 else None
    if same_source_group:
        best_confidence = "high"
    elif distinct_source_group:
        best_confidence = "low"
    else:
        best_confidence = confidence_from_margin(best, next_best)

    result = {}
    for deal, score, reasons, warnings in raw:
        result[str(deal.id)] = DealRecommendation(
            score=score,
            confidence=best_confidence if score == best else "duplicate",
            reasons=reasons,
            warnings=warnings,
        )
    return result


def parse_indices(value: str, max_index: int) -> list[int]:
    selected = []
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


def candidate_groups(subset: str) -> list[tuple[str, list[Deal]]]:
    qs = Deal.objects.all()
    if subset == "source-backed":
        qs = qs.exclude(source_onedrive_id__isnull=True).exclude(source_onedrive_id="")
    else:
        qs = qs.annotate(analysis_count=Count("analyses")).filter(analysis_count__gt=0)

    groups = defaultdict(list)
    for deal in qs.order_by("title", "created_at", "id"):
        key = normalize_deal_title(deal.title)
        if key:
            groups[key].append(deal)
    return [
        (key, deals)
        for key, deals in sorted(groups.items(), key=lambda item: (-len(item[1]), item[0]))
        if len(deals) > 1
    ]


def format_recommendation(recommendation: DealRecommendation) -> str:
    reasons = "; ".join(recommendation.reasons[:5])
    if len(recommendation.reasons) > 5:
        reasons += f"; +{len(recommendation.reasons) - 5} more"
    warning_text = f" warnings={'; '.join(recommendation.warnings)}" if recommendation.warnings else ""
    return f"score={recommendation.score} confidence={recommendation.confidence} reasons={reasons}{warning_text}"


class Command(BaseCommand):
    help = "Interactively review and merge duplicate analyzed/source-backed deals."

    def add_arguments(self, parser):
        parser.add_argument("--subset", choices=["analyzed", "source-backed"], default="analyzed")
        parser.add_argument("--key", help="Only review one normalized duplicate key.")
        parser.add_argument("--limit", type=int, default=0, help="Maximum candidate groups to review.")
        parser.add_argument("--apply", action="store_true", help="Write merges. Default is dry-run.")
        parser.add_argument("--yes", action="store_true", help="With --apply, skip final per-group confirmation.")

    def handle(self, *args, **options):
        groups = candidate_groups(options["subset"])
        if options.get("key"):
            groups = [(key, deals) for key, deals in groups if key == options["key"]]
        if options["limit"]:
            groups = groups[: options["limit"]]

        if not groups:
            self.stdout.write(self.style.SUCCESS("No duplicate deal candidate groups found."))
            return

        mode = "APPLY" if options["apply"] else "DRY-RUN"
        self.stdout.write(f"{mode}: reviewing {len(groups)} {options['subset']} deal duplicate candidate group(s).")
        self.stdout.write("Commands: choose canonical index, 's' skip, 'q' quit. Default canonical is highest score.")

        for group_index, (key, deals) in enumerate(groups, 1):
            self.stdout.write("")
            self.stdout.write(self.style.MIGRATE_HEADING(f"[{group_index}/{len(groups)}] normalized key: {key}"))
            recommendations = recommendations_for_group(deals)
            source_ids = source_id_set(deals)
            if len(source_ids) == 1 and all(deal.source_onedrive_id for deal in deals):
                self.stdout.write(self.style.SUCCESS("  Same OneDrive source folder across all rows: very likely duplicate."))
            elif len(source_ids) > 1:
                self.stdout.write(
                    self.style.WARNING(
                        f"  Multiple distinct OneDrive source folders ({len(source_ids)}): review carefully; may be different deals."
                    )
                )
            best_score = max((rec.score for rec in recommendations.values()), default=None)
            best_indices = []
            for index, deal in enumerate(deals, 1):
                rec = recommendations[str(deal.id)]
                if rec.score == best_score:
                    best_indices.append(index)
                marker = "  [recommended canonical]" if rec.score == best_score else ""
                self.stdout.write(
                    f"  {index}. {deal.title or '(blank)'} | id={deal.id} | "
                    f"phase={deal.current_phase or '-'} | bank={deal.bank.name if deal.bank else '-'} | "
                    f"source={'yes' if deal.source_onedrive_id else 'no'} | "
                    f"analyses={deal.analyses.count()} docs={deal.documents.count()} chunks={deal.chunks.count()}{marker}"
                )
                self.stdout.write(f"     {format_recommendation(rec)}")

            default_canonical = best_indices[0] if len(best_indices) == 1 else 1
            if len(best_indices) > 1:
                self.stdout.write(self.style.WARNING(f"  Tied recommendation: {best_indices}. Review manually."))

            action = input(f"Choose canonical index, 's' to skip, or 'q' to quit [{default_canonical}]: ").strip().lower()
            if action == "q":
                return
            if action == "s":
                continue
            canonical_index = int(action or str(default_canonical))
            if canonical_index < 1 or canonical_index > len(deals):
                raise CommandError("Canonical index out of range.")

            canonical = deals[canonical_index - 1]
            default_duplicates = [idx for idx in range(1, len(deals) + 1) if idx != canonical_index]
            duplicate_answer = input(
                f"Duplicate indices to merge into {canonical_index} "
                f"[{','.join(str(idx) for idx in default_duplicates)}]: "
            ).strip()
            duplicate_indices = parse_indices(
                duplicate_answer or ",".join(str(idx) for idx in default_duplicates),
                len(deals),
            )
            duplicates = [deals[idx - 1] for idx in duplicate_indices if idx != canonical_index]
            if not duplicates:
                self.stdout.write("  No duplicates selected.")
                continue

            self.stdout.write(f"  Canonical: {canonical.title} ({canonical.id})")
            for duplicate in duplicates:
                self.stdout.write(f"  Merge:     {duplicate.title} ({duplicate.id})")

            if not options["apply"]:
                self.stdout.write(self.style.WARNING("  Dry-run only. Re-run with --apply to write this merge."))
                continue
            if not options["yes"]:
                confirm = input("Apply this deal merge? This deletes duplicate deal rows. [y/N]: ").strip().lower()
                if confirm != "y":
                    continue
            for duplicate in duplicates:
                merge_deal_into_canonical(canonical, duplicate)
            self.stdout.write(self.style.SUCCESS("  Deal merge complete."))
