from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q

from ai_orchestrator.services.embedding_processor import EmbeddingService
from deals.models import Deal
from deals.services.bulk_sync_resolution import folder_aliases, normalized_deal_name


REPO_ROOT = Path(__file__).resolve().parents[4]
EXTRACTIONS_DIR = REPO_ROOT / "data" / "extractions"
SUSPICIOUS_TITLE_PATTERNS = (
    "investment report",
    "investment analysis",
)


def is_suspicious_title(title: str) -> bool:
    normalized = (title or "").strip().lower()
    return any(pattern in normalized for pattern in SUSPICIOUS_TITLE_PATTERNS)


class Command(BaseCommand):
    help = "Repair canonical deal titles from extraction folder metadata and optionally refresh embeddings."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write title repairs to the database. Default is dry-run.",
        )
        parser.add_argument(
            "--refresh-embeddings",
            action="store_true",
            help="Refresh deal-level embeddings/profile for repaired deals.",
        )
        parser.add_argument(
            "--deal",
            action="append",
            dest="deals",
            help="Optional extraction folder name to scope the repair. Can be passed multiple times.",
        )

    def handle(self, *args, **options):
        if not EXTRACTIONS_DIR.exists():
            self.stdout.write(self.style.ERROR(f"Extraction directory not found: {EXTRACTIONS_DIR}"))
            return

        target_deals = {value.strip() for value in options["deals"] or [] if value and value.strip()}
        apply_changes = bool(options["apply"])
        refresh_embeddings = bool(options["refresh_embeddings"])
        embed_service = EmbeddingService() if refresh_embeddings else None

        scanned = 0
        repaired = 0
        skipped = 0
        conflicts = 0

        for deal_dir in sorted(EXTRACTIONS_DIR.iterdir()):
            if not deal_dir.is_dir():
                continue
            if target_deals and deal_dir.name not in target_deals:
                continue

            synthesis_path = deal_dir / "DEAL_SYNTHESIS.artifact.json"
            if not synthesis_path.exists():
                continue

            scanned += 1
            artifact = self._load_artifact(synthesis_path)
            canonical_title = normalized_deal_name(deal_dir.name, artifact)
            aliases = folder_aliases(deal_dir.name, artifact)

            matched = self._find_alias_matches(aliases)
            if not matched:
                skipped += 1
                self.stdout.write(f"[SKIP] {deal_dir.name}: no matching deal row found")
                continue

            exact_canonical = [deal for deal in matched if deal.title.strip().lower() == canonical_title.strip().lower()]
            suspicious_matches = [deal for deal in matched if is_suspicious_title(deal.title)]

            if exact_canonical:
                canonical_deal = exact_canonical[0]
                self.stdout.write(f"[OK] {deal_dir.name}: canonical title already present as '{canonical_deal.title}'")
                if len(matched) > 1:
                    self.stdout.write(
                        self.style.WARNING(
                            f"  aliases also match {len(matched) - 1} additional row(s); consider dedupe separately"
                        )
                    )
                continue

            if len(matched) != 1:
                conflicts += 1
                self.stdout.write(
                    self.style.WARNING(
                        f"[CONFLICT] {deal_dir.name}: matched {len(matched)} rows but no exact canonical row; manual review required"
                    )
                )
                for deal in matched:
                    self.stdout.write(f"  - {deal.id} | {deal.title}")
                continue

            deal = matched[0]
            if deal.title == canonical_title:
                self.stdout.write(f"[OK] {deal_dir.name}: title already canonical")
                continue

            if not suspicious_matches and not is_suspicious_title(deal.title):
                conflicts += 1
                self.stdout.write(
                    self.style.WARNING(
                        f"[CONFLICT] {deal_dir.name}: only matched row '{deal.title}' is not obviously synthesized; skipping"
                    )
                )
                continue

            self.stdout.write(f"[REPAIR] {deal.id} | {deal.title} -> {canonical_title}")
            if not apply_changes:
                repaired += 1
                continue

            self._repair_deal_title(deal, canonical_title)
            repaired += 1

            if embed_service:
                embed_service.vectorize_deal(deal)
                embed_service.refresh_deal_profile(deal)
                self.stdout.write("  [EMBED] refreshed deal summary/profile embeddings")

        mode = "APPLY" if apply_changes else "DRY-RUN"
        self.stdout.write("-" * 72)
        self.stdout.write(
            self.style.SUCCESS(
                f"{mode} complete. scanned={scanned} repaired={repaired} skipped={skipped} conflicts={conflicts}"
            )
        )

    def _load_artifact(self, path: Path) -> dict:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def _find_alias_matches(self, aliases: list[str]) -> list[Deal]:
        query = Q()
        normalized_aliases = [alias.strip() for alias in aliases if alias and alias.strip()]
        for alias in normalized_aliases:
            query |= Q(title__iexact=alias)
        if not query:
            return []
        return list(Deal.objects.filter(query).order_by("created_at", "id"))

    @transaction.atomic
    def _repair_deal_title(self, deal: Deal, canonical_title: str):
        deal.title = canonical_title
        deal.save(update_fields=["title"])
