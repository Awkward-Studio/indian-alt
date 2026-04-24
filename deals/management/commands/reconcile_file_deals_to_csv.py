from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from deals.models import Deal
from deals.services.bulk_sync_resolution import (
    _candidate_query_for_aliases,
    _title_tokens,
    _title_match_score,
    folder_aliases,
    load_synthesis_artifact,
)
from deals.services.deal_merge import merge_deal_into_canonical


DEFAULT_BASE_DIR = Path("data/SYNTHESIZED/extractions")
IDENTITY_TOKEN_STOPWORDS = {
    "advisor",
    "advisors",
    "allegro",
    "ambit",
    "bank",
    "capital",
    "dca",
    "deloitte",
    "diagnostic",
    "diagnostics",
    "finance",
    "financial",
    "food",
    "foods",
    "healthcare",
    "hospital",
    "hospitals",
    "idrive",
    "indigoedge",
    "intequant",
    "life",
    "lifesciences",
    "maple",
    "mfi",
    "nuvama",
    "pwc",
    "services",
    "small",
    "spark",
    "sprout",
    "systematix",
    "unitus",
    "vidura",
}


@dataclass
class Candidate:
    deal: Deal
    score: float
    alias: str
    document_count: int
    chunk_count: int
    analysis_count: int
    csv_score: int

    @property
    def has_file_payload(self) -> bool:
        return bool(self.document_count or self.chunk_count or self.analysis_count)

    @property
    def has_csv_payload(self) -> bool:
        return self.csv_score > 0


def csv_import_signal_score(deal: Deal) -> int:
    score = 0
    deal_details = str(deal.deal_details or "")
    if "Source:" in deal_details:
        score += 6
    if "Funding Type:" in deal_details:
        score += 3
    if "Next Steps:" in deal_details:
        score += 2
    for field in ("company_details", "reasons_for_passing", "comments"):
        if str(getattr(deal, field, "") or "").strip():
            score += 1
    return score


class Command(BaseCommand):
    help = "Attach file-backed extraction deals to the matching CSV-imported deal rows."

    def add_arguments(self, parser):
        parser.add_argument("--base-dir", default=str(DEFAULT_BASE_DIR), help="Extraction folder root.")
        parser.add_argument("--folder", action="append", help="Optional extraction folder name to inspect/apply.")
        parser.add_argument("--apply", action="store_true", help="Merge file-backed rows into CSV rows.")
        parser.add_argument("--min-score", type=float, default=0.72, help="Minimum title match score for candidate matches.")
        parser.add_argument(
            "--interactive",
            action="store_true",
            help="For each file-backed deal, list possible target rows and prompt for the row to merge into.",
        )
        parser.add_argument(
            "--candidate-limit",
            type=int,
            default=12,
            help="Maximum possible target rows to show in interactive mode.",
        )
        parser.add_argument(
            "--target-deal",
            help="Manual target deal title or UUID. Requires exactly one --folder.",
        )
        parser.add_argument(
            "--allow-missing",
            action="store_true",
            help="Report unresolved folders without failing. Default behavior is report-only either way.",
        )

    def handle(self, *args, **options):
        base_dir = Path(options["base_dir"])
        requested_folders = set(options["folder"] or [])
        apply = options["apply"]
        min_score = options["min_score"]
        interactive = options["interactive"]
        candidate_limit = options["candidate_limit"]
        target_deal = options.get("target_deal")

        if interactive and target_deal:
            self.stderr.write(self.style.ERROR("--interactive cannot be combined with --target-deal."))
            return

        if target_deal and len(requested_folders) != 1:
            self.stderr.write(self.style.ERROR("--target-deal requires exactly one --folder."))
            return

        if not base_dir.exists():
            self.stderr.write(self.style.ERROR(f"Base directory not found: {base_dir}"))
            return

        folders = [
            path
            for path in sorted(base_dir.iterdir())
            if path.is_dir() and (not requested_folders or path.name in requested_folders)
        ]

        self.stdout.write(
            f"[{'APPLY' if apply else 'DRY-RUN'}] folders={len(folders)} base_dir={base_dir}"
        )

        ok = 0
        merges = 0
        unresolved = 0
        no_file_payload = 0

        for folder in folders:
            artifact = load_synthesis_artifact(folder)
            aliases = folder_aliases(folder.name, artifact)
            candidates = self._find_candidates(aliases, min_score=min_score)
            if target_deal:
                self._handle_manual_target(folder, candidates, target_deal, apply=apply)
                continue
            if interactive:
                result = self._handle_interactive(
                    folder,
                    aliases,
                    candidates,
                    apply=apply,
                    candidate_limit=candidate_limit,
                )
                if result == "merged":
                    merges += 1
                elif result == "ok":
                    ok += 1
                elif result == "no_file_payload":
                    no_file_payload += 1
                elif result == "unresolved":
                    unresolved += 1
                continue
            folder_tokens = self._identity_tokens(folder.name)
            file_candidates = [
                candidate
                for candidate in candidates
                if candidate.has_file_payload and self._shares_folder_identity(candidate.deal.title or "", folder_tokens)
            ]
            csv_candidates = [
                candidate
                for candidate in candidates
                if candidate.has_csv_payload and self._shares_folder_identity(candidate.deal.title or "", folder_tokens)
            ]

            if not candidates:
                unresolved += 1
                self.stdout.write(f"[UNRESOLVED] {folder.name}: no matching deal row aliases={aliases[:4]}")
                continue

            if not file_candidates:
                no_file_payload += 1
                best = candidates[0]
                self.stdout.write(
                    f"[NO-FILE-PAYLOAD] {folder.name}: best={best.deal.title} "
                    f"id={best.deal.id} score={best.score:.2f}"
                )
                continue

            source = sorted(
                file_candidates,
                key=lambda item: (-item.document_count, -item.chunk_count, -item.analysis_count, -item.csv_score),
            )[0]
            target_candidates = [
                candidate
                for candidate in csv_candidates
                if candidate.deal.id != source.deal.id
            ]

            if not target_candidates:
                ok += 1
                self.stdout.write(
                    f"[OK] {folder.name}: attached={source.deal.title} id={source.deal.id} "
                    f"docs={source.document_count} chunks={source.chunk_count} analyses={source.analysis_count}"
                )
                continue

            target = sorted(
                target_candidates,
                key=lambda item: (-item.csv_score, -item.score, item.deal.created_at, str(item.deal.id)),
            )[0]
            merges += 1
            self.stdout.write(
                f"[MERGE{'-DRY-RUN' if not apply else ''}] {folder.name}: "
                f"file_deal={source.deal.title} ({source.deal.id}) -> "
                f"csv_deal={target.deal.title} ({target.deal.id}) "
                f"score={target.score:.2f} docs={source.document_count} chunks={source.chunk_count} analyses={source.analysis_count}"
            )
            if apply:
                merge_deal_into_canonical(target.deal, source.deal)

        self.stdout.write("-" * 72)
        self.stdout.write(
            f"Complete. ok={ok} merges={'applied' if apply else 'planned'}:{merges} "
            f"unresolved={unresolved} no_file_payload={no_file_payload}"
        )

    def _handle_interactive(
        self,
        folder: Path,
        aliases: list[str],
        candidates: list[Candidate],
        *,
        apply: bool,
        candidate_limit: int,
    ) -> str:
        self.stdout.write("")
        self.stdout.write("=" * 88)
        self.stdout.write(f"Folder: {folder.name}")
        self.stdout.write(f"Aliases: {', '.join(aliases[:8])}")

        if not candidates:
            self.stdout.write(f"[UNRESOLVED] {folder.name}: no matching deal rows found.")
            return "unresolved"

        source_candidates = [candidate for candidate in candidates if candidate.has_file_payload]
        if not source_candidates:
            self.stdout.write("[NO-FILE-PAYLOAD] No candidate row has documents/chunks/analysis to move.")
            self.stdout.write("Possible rows:")
            for index, candidate in enumerate(candidates[:candidate_limit], start=1):
                self.stdout.write(self._format_candidate(index, candidate))
            return "no_file_payload"

        source = sorted(
            source_candidates,
            key=lambda item: (-item.document_count, -item.chunk_count, -item.analysis_count, -item.csv_score),
        )[0]
        self.stdout.write("Source row to move documents/chunks/analysis from:")
        self.stdout.write(self._format_candidate(0, source))

        targets = [candidate for candidate in candidates if candidate.deal.id != source.deal.id]
        targets.sort(
            key=lambda item: (
                -item.has_csv_payload,
                item.has_file_payload,
                -item.csv_score,
                -item.score,
                str(item.deal.title or "").lower(),
            )
        )

        if not targets:
            self.stdout.write("[OK] No separate possible target rows. Source appears to be the only matching row.")
            return "ok"

        self.stdout.write("Possible target rows to merge INTO:")
        for index, candidate in enumerate(targets[:candidate_limit], start=1):
            self.stdout.write(self._format_candidate(index, candidate))
        if len(targets) > candidate_limit:
            self.stdout.write(f"... {len(targets) - candidate_limit} more candidates hidden by --candidate-limit")

        while True:
            choice = input("Select target number, Enter/s=skip, q=quit: ").strip().lower()
            if choice in ("", "s", "skip"):
                self.stdout.write(f"[SKIP] {folder.name}")
                return "skipped"
            if choice in ("q", "quit", "exit"):
                raise CommandError("Stopped by user.")
            if not choice.isdigit():
                self.stdout.write("Enter a target number, s to skip, or q to quit.")
                continue
            selected_index = int(choice)
            if selected_index < 1 or selected_index > min(len(targets), candidate_limit):
                self.stdout.write("Selected number is outside the displayed target range.")
                continue
            target = targets[selected_index - 1]
            break

        self.stdout.write(
            f"[INTERACTIVE-MERGE{'-DRY-RUN' if not apply else ''}] {folder.name}: "
            f"file_deal={source.deal.title} ({source.deal.id}) -> "
            f"target={target.deal.title} ({target.deal.id}) "
            f"score={target.score:.2f} docs={source.document_count} "
            f"chunks={source.chunk_count} analyses={source.analysis_count}"
        )
        if apply:
            merge_deal_into_canonical(target.deal, source.deal)
        return "merged"

    def _format_candidate(self, index: int, candidate: Candidate) -> str:
        deal = candidate.deal
        prefix = "  source" if index == 0 else f"  {index}."
        phase = deal.current_phase or "N/A"
        status = deal.deal_status or "N/A"
        fund = deal.fund or "N/A"
        funding_ask = deal.funding_ask or "N/A"
        csv_label = "yes" if candidate.has_csv_payload else "no"
        file_label = "yes" if candidate.has_file_payload else "no"
        return (
            f"{prefix} {deal.title} | id={deal.id} | score={candidate.score:.2f} | "
            f"alias={candidate.alias!r} | csv={csv_label} csv_score={candidate.csv_score} | "
            f"file_payload={file_label} docs={candidate.document_count} chunks={candidate.chunk_count} "
            f"analyses={candidate.analysis_count} | phase={phase} | status={status} | "
            f"fund={fund} | funding_ask={funding_ask}"
        )

    def _handle_manual_target(self, folder: Path, candidates: list[Candidate], target_deal: str, *, apply: bool) -> None:
        source_candidates = [candidate for candidate in candidates if candidate.has_file_payload]
        if not source_candidates:
            self.stdout.write(f"[MANUAL-NO-SOURCE] {folder.name}: no file-backed source deal found")
            return

        source = sorted(
            source_candidates,
            key=lambda item: (-item.document_count, -item.chunk_count, -item.analysis_count),
        )[0]
        target = None
        try:
            target_id = uuid.UUID(str(target_deal))
        except ValueError:
            target_id = None
        if target_id:
            target = Deal.objects.filter(id=target_id).first()
        if not target:
            target = Deal.objects.filter(title__iexact=target_deal).first()
        if not target:
            self.stdout.write(f"[MANUAL-NO-TARGET] {folder.name}: target not found: {target_deal}")
            return
        if target.id == source.deal.id:
            self.stdout.write(f"[MANUAL-OK] {folder.name}: source already equals target {target.title} ({target.id})")
            return

        self.stdout.write(
            f"[MANUAL-MERGE{'-DRY-RUN' if not apply else ''}] {folder.name}: "
            f"file_deal={source.deal.title} ({source.deal.id}) -> "
            f"target={target.title} ({target.id}) docs={source.document_count} "
            f"chunks={source.chunk_count} analyses={source.analysis_count}"
        )
        if apply:
            merge_deal_into_canonical(target, source.deal)

    def _find_candidates(self, aliases: list[str], *, min_score: float) -> list[Candidate]:
        if not aliases:
            return []

        query = Q()
        for alias in aliases:
            query |= Q(title__iexact=alias)
        fuzzy_query = _candidate_query_for_aliases(aliases)
        if fuzzy_query:
            query |= fuzzy_query

        if not query:
            return []

        candidates: list[Candidate] = []
        seen_ids: set[str] = set()
        for deal in Deal.objects.filter(query).order_by("created_at", "id"):
            if str(deal.id) in seen_ids:
                continue
            best_score = 0.0
            best_alias = ""
            for alias in aliases:
                score = _title_match_score(alias, deal.title or "")
                if score > best_score:
                    best_score = score
                    best_alias = alias
            if best_score < min_score:
                continue
            seen_ids.add(str(deal.id))
            candidates.append(
                Candidate(
                    deal=deal,
                    score=best_score,
                    alias=best_alias,
                    document_count=deal.documents.count(),
                    chunk_count=deal.chunks.count(),
                    analysis_count=deal.analyses.count(),
                    csv_score=csv_import_signal_score(deal),
                )
            )

        candidates.sort(
            key=lambda item: (
                -item.has_csv_payload,
                -item.has_file_payload,
                -item.csv_score,
                -item.document_count,
                -item.chunk_count,
                -item.analysis_count,
                -item.score,
            )
        )
        return candidates

    def _shares_folder_identity(self, title: str, folder_tokens: set[str]) -> bool:
        if not folder_tokens:
            return False
        title_tokens = self._identity_tokens(title)
        return bool(folder_tokens & title_tokens)

    def _identity_tokens(self, value: str) -> set[str]:
        return {
            token
            for token in _title_tokens(value)
            if token not in IDENTITY_TOKEN_STOPWORDS
        }
