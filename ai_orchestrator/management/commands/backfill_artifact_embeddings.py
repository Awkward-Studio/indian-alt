import json
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from ai_orchestrator.services.embedding_processor import EmbeddingService
from deals.models import (
    ChunkingStatus,
    Deal,
    DealDocument,
    DocumentType,
    ExtractionMode,
    TranscriptionStatus,
)


class Command(BaseCommand):
    help = (
        "Backfill canonical DealDocument rows and chunk embeddings from "
        "data/extractions/*.artifact.json files."
    )

    SKIP_EXTENSIONS = {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".svg",
        ".webp",
        ".ico",
        ".bmp",
    }

    def add_arguments(self, parser):
        parser.add_argument("--deal", type=str, help="Only process one deal folder/title match.")
        parser.add_argument("--limit", type=int, default=0, help="Optional max number of artifact files to inspect.")
        parser.add_argument("--force", action="store_true", help="Rebuild DealDocument chunks even if already indexed.")
        parser.add_argument("--profiles-only", action="store_true", help="Only refresh deal retrieval profiles.")
        parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing to the database.")
        parser.add_argument(
            "--include-images",
            action="store_true",
            help="Include image-like artifact filenames instead of skipping them by default.",
        )

    @staticmethod
    def _folder_to_deal_title(folder_name: str) -> str:
        return folder_name.replace("_", " ").replace("-", "/")

    @staticmethod
    def _normalize_document_type(raw_value: str | None) -> str:
        candidate = (raw_value or "").strip()
        valid = {choice for choice, _ in DocumentType.choices}
        return candidate if candidate in valid else DocumentType.OTHER

    def handle(self, *args, **options):
        base_dir = Path("data/extractions")
        if not base_dir.exists():
            self.stderr.write(self.style.ERROR(f"{base_dir} does not exist"))
            return

        embed_service = EmbeddingService()
        deal_filter = (options.get("deal") or "").strip().lower()
        limit = int(options.get("limit") or 0)
        force = bool(options.get("force"))
        profiles_only = bool(options.get("profiles_only"))
        dry_run = bool(options.get("dry_run"))
        include_images = bool(options.get("include_images"))

        deal_dirs = [path for path in sorted(base_dir.iterdir()) if path.is_dir()]
        if deal_filter:
            deal_dirs = [
                path for path in deal_dirs
                if deal_filter in path.name.lower() or deal_filter in self._folder_to_deal_title(path.name).lower()
            ]

        inspected = 0
        doc_creates = 0
        doc_updates = 0
        chunk_rebuilds = 0
        profile_refreshes = 0
        skipped_assets = 0

        for deal_dir in deal_dirs:
            deal_title = self._folder_to_deal_title(deal_dir.name)
            deal = Deal.objects.filter(title=deal_title).first()
            if not deal:
                self.stdout.write(self.style.WARNING(f"[SKIP] Missing Deal row for folder {deal_dir.name}"))
                continue

            if profiles_only:
                if dry_run:
                    self.stdout.write(f"[DRY RUN] Would refresh retrieval profile for deal: {deal.title}")
                    profile_refreshes += 1
                else:
                    if embed_service.refresh_deal_profile(deal):
                        profile_refreshes += 1
                continue

            artifact_files = [
                path for path in sorted(deal_dir.glob("*.artifact.json"))
                if "DEAL_SYNTHESIS" not in path.name
            ]

            for artifact_path in artifact_files:
                if limit and inspected >= limit:
                    break
                inspected += 1

                file_name = artifact_path.name.replace(".artifact.json", "")
                suffix = Path(file_name).suffix.lower()
                if suffix in self.SKIP_EXTENSIONS and not include_images:
                    self.stdout.write(f"[SKIP] Asset-like artifact filename: {artifact_path.name}")
                    skipped_assets += 1
                    continue
                try:
                    artifact = json.loads(artifact_path.read_text())
                except Exception as exc:
                    self.stdout.write(self.style.ERROR(f"[ERROR] {artifact_path}: {exc}"))
                    continue

                normalized_text = (artifact.get("normalized_text") or "").strip()
                if not normalized_text:
                    self.stdout.write(self.style.WARNING(f"[SKIP] {artifact_path.name} has no normalized_text"))
                    continue
                if normalized_text.lower().startswith("there is no text") and not include_images:
                    self.stdout.write(f"[SKIP] Low-signal OCR artifact: {artifact_path.name}")
                    skipped_assets += 1
                    continue

                document_type = self._normalize_document_type(artifact.get("document_type"))
                existing = DealDocument.objects.filter(deal=deal, title=file_name).first()
                will_create = existing is None
                should_rechunk = force or will_create or not bool(existing and existing.is_indexed)

                if dry_run:
                    action = "create" if will_create else "update"
                    self.stdout.write(
                        f"[DRY RUN] Would {action} DealDocument for {deal.title} :: {file_name} "
                        f"(type={document_type}, rechunk={'yes' if should_rechunk else 'no'})"
                    )
                    doc_creates += 1 if will_create else 0
                    doc_updates += 0 if will_create else 1
                    chunk_rebuilds += 1 if should_rechunk else 0
                    continue

                with transaction.atomic():
                    deal_document, created = DealDocument.objects.update_or_create(
                        deal=deal,
                        title=file_name,
                        defaults={
                            "document_type": document_type,
                            "extracted_text": normalized_text,
                            "normalized_text": normalized_text,
                            "evidence_json": artifact,
                            "source_map_json": artifact.get("source_map") or {},
                            "table_json": artifact.get("tables_summary") or [],
                            "key_metrics_json": artifact.get("metrics") or [],
                            "reasoning": artifact.get("reasoning") or "",
                            "extraction_mode": ExtractionMode.FALLBACK_TEXT,
                            "transcription_status": TranscriptionStatus.COMPLETE,
                            "chunking_status": ChunkingStatus.NOT_CHUNKED if should_rechunk else (existing.chunking_status if existing else ChunkingStatus.NOT_CHUNKED),
                            "last_transcribed_at": timezone.now(),
                        },
                    )
                    if should_rechunk:
                        embed_service.vectorize_document(deal_document)

                doc_creates += 1 if created else 0
                doc_updates += 0 if created else 1
                chunk_rebuilds += 1 if should_rechunk else 0

            if limit and inspected >= limit:
                break

            if dry_run:
                self.stdout.write(f"[DRY RUN] Would refresh retrieval profile for deal: {deal.title}")
                profile_refreshes += 1
            else:
                if embed_service.refresh_deal_profile(deal):
                    profile_refreshes += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Artifact backfill preview complete. inspected={inspected} "
                f"doc_creates={doc_creates} doc_updates={doc_updates} "
                f"chunk_rebuilds={chunk_rebuilds} profile_refreshes={profile_refreshes} "
                f"skipped_assets={skipped_assets} dry_run={'yes' if dry_run else 'no'}"
            )
        )
