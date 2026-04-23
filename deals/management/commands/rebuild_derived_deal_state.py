from __future__ import annotations

import argparse
import gc
import json
from dataclasses import dataclass
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count, Q

from ai_orchestrator.models import DealRetrievalProfile, DocumentChunk
from ai_orchestrator.services.embedding_processor import EmbeddingService
from deals.models import Deal
from deals.services.bulk_sync_resolution import folder_aliases, load_synthesis_artifact, normalize_placeholder, resolve_existing_deal
from deals.services.synthesis_rebuild import load_investment_report_text, refresh_deal_embeddings, sync_synthesis_artifact


DEFAULT_EXTRACTIONS_DIR = Path(__file__).resolve().parents[3] / "data" / "extractions"
SUSPICIOUS_TITLE_PATTERNS = (
    "investment report",
    "investment analysis",
)
RESET_NULL_FIELDS = (
    "deal_summary",
    "funding_ask",
    "funding_ask_for",
    "industry",
    "sector",
    "city",
    "state",
    "country",
    "priority",
    "deal_details",
    "company_details",
    "priority_rationale",
    "legacy_investment_bank",
)
RESET_EMPTY_LIST_FIELDS = (
    "themes",
    "other_contacts",
)


@dataclass
class TitleRepairDecision:
    status: str
    message: str
    canonical_title: str | None
    candidate: Deal | None = None


def is_suspicious_title(title: str) -> bool:
    normalized = (title or "").strip().lower()
    return any(pattern in normalized for pattern in SUSPICIOUS_TITLE_PATTERNS)


class Command(BaseCommand):
    help = "Reset derived deal state and rebuild synthesis-driven analyses and deal-level retrieval artifacts."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Apply the rebuild. Default mode is dry-run.",
        )
        parser.add_argument(
            "--deal",
            action="append",
            dest="deals",
            help="Optional extraction folder name or canonical deal title to scope the rebuild. Can be passed multiple times.",
        )
        parser.add_argument(
            "--base-dir",
            default=str(DEFAULT_EXTRACTIONS_DIR),
            help="Extraction directory containing DEAL_SYNTHESIS.artifact.json files.",
        )
        parser.add_argument(
            "--skip-title-repair",
            action="store_true",
            help="Skip the canonical title repair pass.",
        )
        parser.add_argument(
            "--preserve-analysis-history",
            action="store_true",
            help="Append rebuilt synthesis as a new analysis version instead of deleting existing analysis history first.",
        )
        parser.add_argument(
            "--rebuild-embeddings",
            action=argparse.BooleanOptionalAction,
            default=True,
            help="Refresh deal-summary chunks and retrieval profiles after rebuilding. Enabled by default.",
        )
        parser.add_argument(
            "--report-json",
            action="store_true",
            help="Print the final summary as JSON.",
        )
        parser.add_argument(
            "--summary-only",
            action="store_true",
            help="Omit per-deal details from the final JSON report.",
        )
        parser.add_argument(
            "--offset",
            type=int,
            default=0,
            help="Skip the first N matched extraction directories before processing.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Process at most N matched extraction directories. Default 0 means no limit.",
        )
        parser.add_argument(
            "--progress-every",
            type=int,
            default=10,
            help="Emit a progress log after every N processed deals. Set to 0 to disable.",
        )
        parser.add_argument(
            "--prune-unmatched-deals",
            action="store_true",
            help="Delete deals that are not resolved from any synthesis artifact. Dry-run unless --apply is also passed.",
        )
        parser.add_argument(
            "--prune-only",
            action="store_true",
            help="Only run unmatched deal pruning; skip synthesis rebuild.",
        )

    def handle(self, *args, **options):
        apply_changes = bool(options["apply"])
        dry_run = not apply_changes
        preserve_history = bool(options["preserve_analysis_history"])
        rebuild_embeddings = bool(options["rebuild_embeddings"])
        skip_title_repair = bool(options["skip_title_repair"])
        report_json = bool(options["report_json"])
        summary_only = bool(options["summary_only"])
        base_dir = Path(options["base_dir"]).expanduser().resolve()
        offset = max(int(options.get("offset") or 0), 0)
        limit = max(int(options.get("limit") or 0), 0)
        progress_every = max(int(options.get("progress_every") or 0), 0)
        prune_unmatched_deals = bool(options["prune_unmatched_deals"])
        prune_only = bool(options["prune_only"])
        target_filters = {value.strip().lower() for value in options.get("deals") or [] if value and value.strip()}

        if not base_dir.exists():
            self.stdout.write(self.style.ERROR(f"Extraction directory not found: {base_dir}"))
            return

        all_artifact_deal_dirs = []
        for deal_dir in sorted(base_dir.iterdir()):
            if not deal_dir.is_dir():
                continue
            artifact = load_synthesis_artifact(deal_dir)
            if not artifact:
                continue
            aliases = folder_aliases(deal_dir.name, artifact)
            canonical_title = aliases[0] if aliases else deal_dir.name
            all_artifact_deal_dirs.append((deal_dir, artifact, canonical_title, aliases))

        deal_dirs = []
        for deal_dir, artifact, canonical_title, aliases in all_artifact_deal_dirs:
            if target_filters and not self._matches_target_filters(target_filters, deal_dir.name, aliases):
                continue
            deal_dirs.append((deal_dir, artifact, canonical_title))

        if offset:
            deal_dirs = deal_dirs[offset:]
        if limit:
            deal_dirs = deal_dirs[:limit]

        summary = {
            "mode": "apply" if apply_changes else "dry-run",
            "base_dir": str(base_dir),
            "offset": offset,
            "limit": limit,
            "artifact_deals_total": len(all_artifact_deal_dirs),
            "scoped_deals": len(deal_dirs),
            "total_deals_before_prune": Deal.objects.count(),
            "prune_unmatched_deals": prune_unmatched_deals,
            "prune_only": prune_only,
            "matched_deals_for_prune": 0,
            "unmatched_deals": 0,
            "unmatched_deals_deleted": 0,
            "title_repairs": 0,
            "title_conflicts": 0,
            "title_skipped": 0,
            "missing_deals": 0,
            "rebuilt": 0,
            "analyses_deleted": 0,
            "analyses_created": 0,
            "retrieval_profiles_deleted": 0,
            "retrieval_profiles_rebuilt": 0,
            "derived_chunks_deleted": 0,
            "derived_chunks_rebuilt": 0,
            "embedding_refreshes": 0,
            "conflicts": [],
            "missing": [],
            "unmatched": [],
            "details": [],
        }

        if prune_unmatched_deals:
            prune_summary = self._prune_unmatched_deals(
                all_artifact_deal_dirs,
                dry_run=dry_run,
            )
            summary.update(prune_summary)
            for item in prune_summary["unmatched"]:
                self.stdout.write(
                    self.style.WARNING(
                        f"[PRUNE-CANDIDATE] {item['title']} | id={item['id']} | "
                        f"documents={item['documents']} | chunks={item['chunks']} | analyses={item['analyses']}"
                    )
                )

            if prune_only:
                self._write_report(summary, report_json=report_json, summary_only=summary_only)
                return

        self.stdout.write(
            f"[{summary['mode'].upper()}] scoped_deals={len(deal_dirs)} base_dir={base_dir}"
        )

        embed_service = None
        processed_count = 0
        for deal_dir, artifact, canonical_title in deal_dirs:
            title_repair_candidate = None
            if not skip_title_repair:
                repair = self._plan_title_repair(deal_dir.name, artifact)
                if repair.status == "repair":
                    summary["title_repairs"] += 1
                    title_repair_candidate = repair.candidate
                    self.stdout.write(f"[TITLE-REPAIR] {repair.message}")
                    if apply_changes and repair.candidate is not None and repair.canonical_title:
                        repair.candidate.title = repair.canonical_title
                        repair.candidate.save(update_fields=["title"])
                elif repair.status == "conflict":
                    summary["title_conflicts"] += 1
                    summary["conflicts"].append(repair.message)
                    self.stdout.write(self.style.WARNING(f"[TITLE-CONFLICT] {repair.message}"))
                else:
                    summary["title_skipped"] += 1

            resolution = resolve_existing_deal(deal_dir.name, artifact)
            resolved_deal = resolution.deal or title_repair_candidate
            if not resolved_deal:
                summary["missing_deals"] += 1
                summary["missing"].append(canonical_title)
                self.stdout.write(self.style.WARNING(f"[SKIP] {canonical_title}: no existing base deal row matched"))
                continue

            with transaction.atomic():
                deal = resolved_deal
                detail = {
                    "deal_id": str(deal.id),
                    "deal_title": deal.title,
                    "canonical_title": resolution.canonical_title,
                    "analysis_rows_before": deal.analyses.count(),
                    "deal_summary_chunks_before": DocumentChunk.objects.filter(
                        deal=deal,
                        source_type="deal_summary",
                    ).count(),
                    "retrieval_profile_before": DealRetrievalProfile.objects.filter(deal=deal).count(),
                }

                if preserve_history:
                    deleted_analysis_count = 0
                else:
                    deleted_analysis_count = self._delete_analysis_rows(deal, dry_run=dry_run)
                    summary["analyses_deleted"] += deleted_analysis_count

                deleted_chunk_count = self._delete_derived_chunks(deal, dry_run=dry_run)
                deleted_profile_count = self._delete_retrieval_profile(deal, dry_run=dry_run)
                summary["derived_chunks_deleted"] += deleted_chunk_count
                summary["retrieval_profiles_deleted"] += deleted_profile_count

                reset_fields = self._reset_deal_fields(deal, dry_run=dry_run)
                detail["reset_fields"] = reset_fields

                investment_report_text, investment_report_name = load_investment_report_text(deal_dir)
                status, message, synthesis_applied = sync_synthesis_artifact(
                    deal,
                    artifact,
                    investment_report_text=investment_report_text,
                    investment_report_path=investment_report_name,
                    dry_run=dry_run,
                    preserve_history=preserve_history,
                )

                if dry_run:
                    created_analysis_count = 1 if synthesis_applied else 0
                else:
                    created_analysis_count = deal.analyses.count() - detail["analysis_rows_before"] + deleted_analysis_count
                    created_analysis_count = max(created_analysis_count, 0)
                summary["analyses_created"] += created_analysis_count

                rebuilt_summary_chunks = 0
                rebuilt_profiles = 0
                if rebuild_embeddings:
                    if dry_run:
                        rebuilt_summary_chunks = 1 if normalize_placeholder(investment_report_text or (artifact.get("portable_deal_data") or {}).get("analyst_report")) else 0
                        rebuilt_profiles = 1
                    else:
                        if embed_service is None:
                            embed_service = EmbeddingService()
                        before_chunks = DocumentChunk.objects.filter(deal=deal, source_type="deal_summary").count()
                        before_profiles = DealRetrievalProfile.objects.filter(deal=deal).count()
                        summary_embedded, profile_refreshed = refresh_deal_embeddings(deal, embed_service=embed_service)
                        after_chunks = DocumentChunk.objects.filter(deal=deal, source_type="deal_summary").count()
                        after_profiles = DealRetrievalProfile.objects.filter(deal=deal).count()
                        rebuilt_summary_chunks = max(after_chunks - before_chunks, 0)
                        rebuilt_profiles = max(after_profiles - before_profiles, 0)
                        if summary_embedded or profile_refreshed:
                            summary["embedding_refreshes"] += 1

                summary["derived_chunks_rebuilt"] += rebuilt_summary_chunks
                summary["retrieval_profiles_rebuilt"] += rebuilt_profiles
                summary["rebuilt"] += 1

                detail.update(
                    {
                        "status": status,
                        "message": message,
                        "analysis_rows_after": (detail["analysis_rows_before"] if dry_run else deal.analyses.count()),
                        "deal_summary_chunks_after": (
                            detail["deal_summary_chunks_before"] if dry_run
                            else DocumentChunk.objects.filter(deal=deal, source_type="deal_summary").count()
                        ),
                        "retrieval_profile_after": (
                            detail["retrieval_profile_before"] if dry_run
                            else DealRetrievalProfile.objects.filter(deal=deal).count()
                        ),
                    }
                )
                summary["details"].append(detail)
                self.stdout.write(f"[{status}] {deal.title}: {message}")
                processed_count += 1

                if progress_every and processed_count % progress_every == 0:
                    self.stdout.write(
                        f"[PROGRESS] processed={processed_count}/{summary['scoped_deals']} "
                        f"rebuilt={summary['rebuilt']} missing={summary['missing_deals']} "
                        f"title_repairs={summary['title_repairs']}"
                    )

            gc.collect()

        self._write_report(summary, report_json=report_json, summary_only=summary_only)

    def _write_report(self, summary: dict, *, report_json: bool, summary_only: bool) -> None:
        if report_json:
            report = dict(summary)
            if summary_only:
                report["details"] = []
            self.stdout.write(json.dumps(report, indent=2, default=str))
            return

        self.stdout.write("-" * 72)
        self.stdout.write(
            self.style.SUCCESS(
                "Complete. "
                + " ".join(
                    [
                        f"artifact_deals_total={summary['artifact_deals_total']}",
                        f"scoped_deals={summary['scoped_deals']}",
                        f"unmatched_deals={summary['unmatched_deals']}",
                        f"unmatched_deals_deleted={summary['unmatched_deals_deleted']}",
                        f"title_repairs={summary['title_repairs']}",
                        f"title_conflicts={summary['title_conflicts']}",
                        f"missing_deals={summary['missing_deals']}",
                        f"rebuilt={summary['rebuilt']}",
                        f"analyses_deleted={summary['analyses_deleted']}",
                        f"analyses_created={summary['analyses_created']}",
                        f"derived_chunks_deleted={summary['derived_chunks_deleted']}",
                        f"derived_chunks_rebuilt={summary['derived_chunks_rebuilt']}",
                        f"retrieval_profiles_deleted={summary['retrieval_profiles_deleted']}",
                        f"retrieval_profiles_rebuilt={summary['retrieval_profiles_rebuilt']}",
                        f"embedding_refreshes={summary['embedding_refreshes']}",
                    ]
                )
            )
        )
        if summary["conflicts"]:
            self.stdout.write(self.style.WARNING("Conflicts:"))
            for item in summary["conflicts"]:
                self.stdout.write(f"  - {item}")

    def _matches_target_filters(self, target_filters: set[str], folder_name: str, aliases: list[str]) -> bool:
        options = {folder_name.strip().lower()}
        options.update(alias.strip().lower() for alias in aliases if alias and alias.strip())
        return bool(options & target_filters)

    def _prune_unmatched_deals(self, artifact_deal_dirs: list[tuple[Path, dict, str, list[str]]], *, dry_run: bool) -> dict:
        matched_ids: set[str] = set()
        missing_artifacts: list[str] = []
        for _deal_dir, _artifact, canonical_title, aliases in artifact_deal_dirs:
            deal_id = self._resolve_prune_deal_id(aliases)
            if deal_id:
                matched_ids.add(str(deal_id))
            else:
                missing_artifacts.append(canonical_title)

        unmatched_queryset = (
            Deal.objects.exclude(id__in=matched_ids)
            .annotate(
                document_count=Count("documents", distinct=True),
                chunk_count=Count("chunks", distinct=True),
                analysis_count=Count("analyses", distinct=True),
            )
            .order_by("title", "created_at", "id")
        )
        unmatched = []
        for deal in unmatched_queryset:
            unmatched.append(
                {
                    "id": str(deal.id),
                    "title": deal.title,
                    "documents": deal.document_count,
                    "chunks": deal.chunk_count,
                    "analyses": deal.analysis_count,
                }
            )

        deleted_count = 0
        if not dry_run and unmatched:
            deleted_count = len(unmatched)
            unmatched_queryset.delete()

        return {
            "matched_deals_for_prune": len(matched_ids),
            "unmatched_deals": len(unmatched),
            "unmatched_deals_deleted": deleted_count,
            "missing": missing_artifacts,
            "missing_deals": len(missing_artifacts),
            "unmatched": unmatched,
        }

    def _resolve_prune_deal_id(self, aliases: list[str]) -> str | None:
        for alias in aliases:
            normalized_alias = (alias or "").strip()
            if not normalized_alias:
                continue
            deal_id = (
                Deal.objects.filter(title__iexact=normalized_alias)
                .order_by("created_at", "id")
                .values_list("id", flat=True)
                .first()
            )
            if deal_id:
                return str(deal_id)
        return None

    def _find_title_candidates(self, aliases: list[str], canonical_title: str | None) -> list[Deal]:
        normalized_aliases = [alias.strip() for alias in aliases if alias and alias.strip()]
        query = Q()
        for alias in normalized_aliases:
            query |= Q(title__iexact=alias)

        if canonical_title:
            normalized = canonical_title.strip()
            if normalized:
                query |= Q(title__icontains=normalized)

        if not query:
            return []

        candidates = list(Deal.objects.filter(query).order_by("created_at", "id"))
        if canonical_title:
            canonical_lower = canonical_title.strip().lower()
            filtered = []
            for deal in candidates:
                title = (deal.title or "").strip().lower()
                if title == canonical_lower or canonical_lower in title:
                    filtered.append(deal)
            if filtered:
                return filtered
        return candidates

    def _plan_title_repair(self, folder_name: str, artifact: dict) -> TitleRepairDecision:
        aliases = folder_aliases(folder_name, artifact)
        canonical_title = aliases[0] if aliases else None
        if not canonical_title:
            return TitleRepairDecision("skip", f"{folder_name}: no canonical title resolved", canonical_title)

        matched = self._find_title_candidates(aliases, canonical_title)
        if not matched:
            return TitleRepairDecision("skip", f"{folder_name}: no matching deal row found", canonical_title)

        exact_canonical = [
            deal for deal in matched
            if (deal.title or "").strip().lower() == canonical_title.strip().lower()
        ]
        if exact_canonical:
            return TitleRepairDecision("skip", f"{folder_name}: canonical title already present", canonical_title)

        suspicious_matches = [deal for deal in matched if is_suspicious_title(deal.title or "")]
        if len(suspicious_matches) == 1 and len(matched) == 1:
            deal = suspicious_matches[0]
            return TitleRepairDecision(
                "repair",
                f"{folder_name}: {deal.title} -> {canonical_title}",
                canonical_title,
                candidate=deal,
            )

        if len(suspicious_matches) == 1 and len(matched) > 1:
            deal = suspicious_matches[0]
            return TitleRepairDecision(
                "repair",
                f"{folder_name}: selected suspicious title {deal.title} -> {canonical_title}",
                canonical_title,
                candidate=deal,
            )

        if len(matched) == 1:
            deal = matched[0]
            return TitleRepairDecision(
                "skip",
                f"{folder_name}: current title kept ({deal.title})",
                canonical_title,
                candidate=deal,
            )

        return TitleRepairDecision(
            "conflict",
            f"{folder_name}: matched {len(matched)} rows without a unique suspicious candidate",
            canonical_title,
        )

    def _delete_analysis_rows(self, deal: Deal, *, dry_run: bool) -> int:
        count = deal.analyses.count()
        if not dry_run and count:
            deal.analyses.all().delete()
        return count

    def _delete_derived_chunks(self, deal: Deal, *, dry_run: bool) -> int:
        queryset = DocumentChunk.objects.filter(deal=deal, source_type="deal_summary")
        count = queryset.count()
        if not dry_run and count:
            queryset.delete()
        return count

    def _delete_retrieval_profile(self, deal: Deal, *, dry_run: bool) -> int:
        queryset = DealRetrievalProfile.objects.filter(deal=deal)
        count = queryset.count()
        if not dry_run and count:
            queryset.delete()
        return count

    @transaction.atomic
    def _reset_deal_fields(self, deal: Deal, *, dry_run: bool) -> list[str]:
        changed_fields = []
        for field in RESET_NULL_FIELDS:
            if getattr(deal, field) is not None:
                setattr(deal, field, None)
                changed_fields.append(field)
        for field in RESET_EMPTY_LIST_FIELDS:
            current_value = getattr(deal, field)
            if current_value not in ([], None):
                setattr(deal, field, [])
                changed_fields.append(field)

        if deal.bank_id is not None:
            deal.bank = None
            changed_fields.append("bank")
        if deal.primary_contact_id is not None:
            deal.primary_contact = None
            changed_fields.append("primary_contact")
        if deal.is_indexed:
            deal.is_indexed = False
            changed_fields.append("is_indexed")

        if not dry_run:
            if changed_fields:
                deal.save(update_fields=list(dict.fromkeys(changed_fields)))
            deal.additional_contacts.clear()
        return list(dict.fromkeys(changed_fields))
