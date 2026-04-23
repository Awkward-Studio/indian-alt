from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deals.models import Deal


NULL_MARKERS = {
    "",
    "not specified",
    "not identified",
    "none",
    "null",
    "unknown",
    "n/a",
    "na",
}


def normalize_placeholder(value: Any):
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    cleaned = value.strip()
    if not cleaned:
        return None
    if cleaned.lower() in NULL_MARKERS:
        return None
    return cleaned


def normalized_deal_name(folder_name: str, artifact_data: dict[str, Any] | None):
    artifact_name = normalize_placeholder((artifact_data or {}).get("deal_name"))
    if artifact_name:
        return artifact_name

    portable = (artifact_data or {}).get("portable_deal_data") or {}
    model_data = portable.get("deal_model_data") or {}
    synthesized_title = normalize_placeholder(model_data.get("title"))
    if synthesized_title:
        return synthesized_title

    pretty = folder_name.replace("_-_", " - ").replace("_", " ")
    return pretty.strip()


def synthesis_canonical_title(artifact_data: dict[str, Any] | None, folder_name: str):
    return normalized_deal_name(folder_name, artifact_data)


def folder_aliases(folder_name: str, artifact_data: dict[str, Any] | None) -> list[str]:
    aliases: list[str] = []

    canonical_title = synthesis_canonical_title(artifact_data, folder_name)
    if canonical_title:
        aliases.append(canonical_title)

    artifact_name = normalize_placeholder((artifact_data or {}).get("deal_name"))
    if artifact_name and artifact_name not in aliases:
        aliases.append(artifact_name)

    folder_pretty = normalized_deal_name(folder_name, artifact_data)
    if folder_pretty and folder_pretty not in aliases:
        aliases.append(folder_pretty)

    legacy_pretty = folder_name.replace("_", " ").replace("-", "/").strip()
    if legacy_pretty and legacy_pretty not in aliases:
        aliases.append(legacy_pretty)

    portable = (artifact_data or {}).get("portable_deal_data") or {}
    model_data = portable.get("deal_model_data") or {}
    synthesized_title = normalize_placeholder(model_data.get("title"))
    if synthesized_title and synthesized_title not in aliases:
        aliases.append(synthesized_title)

    return aliases


def load_synthesis_artifact(deal_dir: Path) -> dict[str, Any] | None:
    synthesis_path = deal_dir / "DEAL_SYNTHESIS.artifact.json"
    if not synthesis_path.exists():
        return None
    with open(synthesis_path, "r", encoding="utf-8") as handle:
        return json.load(handle)


@dataclass
class DealResolution:
    canonical_title: str | None
    aliases: list[str]
    deal: Deal | None
    duplicates: list[Deal]
    matched_by: str | None


def _deal_rank_key(deal: Deal):
    document_count = getattr(deal, "document_count", 0) or 0
    chunk_count = getattr(deal, "chunk_count", 0) or 0
    analysis_count = getattr(deal, "analysis_count", 0) or 0
    created_at = deal.created_at.isoformat() if deal.created_at else ""
    return (-document_count, -chunk_count, -analysis_count, created_at, str(deal.id))


def resolve_existing_deal(folder_name: str, artifact_data: dict[str, Any] | None) -> DealResolution:
    aliases = folder_aliases(folder_name, artifact_data)
    canonical_title = aliases[0] if aliases else None

    matched_by = None
    matches: list[Deal] = []
    seen_ids: set[str] = set()

    for alias in aliases:
        queryset = (
            Deal.objects.filter(title__iexact=alias)
            .prefetch_related("documents", "chunks", "analyses")
            .order_by("created_at", "id")
        )
        found = list(queryset)
        if found and matched_by is None:
            matched_by = alias
        for deal in found:
            deal_id = str(deal.id)
            if deal_id in seen_ids:
                continue
            deal.document_count = deal.documents.count()
            deal.chunk_count = deal.chunks.count()
            deal.analysis_count = deal.analyses.count()
            matches.append(deal)
            seen_ids.add(deal_id)

    if not matches:
        return DealResolution(
            canonical_title=canonical_title,
            aliases=aliases,
            deal=None,
            duplicates=[],
            matched_by=None,
        )

    matches.sort(key=_deal_rank_key)
    canonical = matches[0]
    duplicates = matches[1:]
    return DealResolution(
        canonical_title=canonical_title,
        aliases=aliases,
        deal=canonical,
        duplicates=duplicates,
        matched_by=matched_by,
    )
