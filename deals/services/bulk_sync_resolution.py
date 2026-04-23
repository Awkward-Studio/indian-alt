from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from django.db.models import Q

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
TITLE_MATCH_STOPWORDS = {
    "a",
    "an",
    "and",
    "analysis",
    "company",
    "deal",
    "equity",
    "for",
    "fund",
    "growth",
    "india",
    "indian",
    "investment",
    "limited",
    "ltd",
    "opportunity",
    "private",
    "project",
    "pvt",
    "report",
    "series",
    "the",
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


def _normalize_match_text(value: str | None) -> str:
    text = (value or "").lower()
    # Remove "Project " prefix if it exists at the start
    text = re.sub(r"^project\s+", "", text)
    # Remove common suffixes that clutter folder names and prevent matches
    text = re.sub(r"[\s_/-]+(intequant|advisors|growth|round|series|investment|opportunity|private|limited|ltd).*", "", text)
    # Standard normalization
    normalized = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(normalized.split())


def _title_tokens(value: str | None) -> set[str]:
    normalized = _normalize_match_text(value)
    tokens = {
        token
        for token in normalized.split()
        if len(token) >= 3 and token not in TITLE_MATCH_STOPWORDS
    }
    # Special case: Keep short numeric tokens like "777" which are highly unique
    numeric_tokens = set(re.findall(r"\d+", (value or "").lower()))
    return tokens | numeric_tokens


def _title_match_score(alias: str, title: str) -> float:
    alias_norm = _normalize_match_text(alias)
    title_norm = _normalize_match_text(title)
    if not alias_norm or not title_norm:
        return 0.0

    if alias_norm == title_norm:
        return 1.0

    alias_tokens = _title_tokens(alias)
    title_tokens = _title_tokens(title)
    if alias_tokens and alias_tokens.issubset(title_tokens):
        # Short folder identities like "Belstar" should bind to the only
        # title containing that distinctive token.
        return 0.92

    if alias_norm in title_norm or title_norm in alias_norm:
        return 0.9

    if not alias_tokens or not title_tokens:
        return 0.0

    overlap = alias_tokens & title_tokens
    if not overlap:
        return 0.0

    containment = len(overlap) / len(alias_tokens)
    jaccard = len(overlap) / len(alias_tokens | title_tokens)
    if containment >= 0.75:
        return 0.75 + min(jaccard, 0.2)
    if containment >= 0.5 and len(overlap) >= 2:
        return 0.55 + min(jaccard, 0.2)
    return 0.0


def _candidate_query_for_aliases(aliases: list[str]) -> Q:
    query = Q()
    for alias in aliases:
        for token in sorted(_title_tokens(alias)):
            query |= Q(title__icontains=token)
    return query


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
        fuzzy_query = _candidate_query_for_aliases(aliases)
        fuzzy_candidates = []
        if fuzzy_query:
            fuzzy_candidates = list(
                Deal.objects.filter(fuzzy_query)
                .prefetch_related("documents", "chunks", "analyses")
                .order_by("created_at", "id")
            )

        scored_candidates: list[tuple[float, Deal, str]] = []
        seen_fuzzy_ids: set[str] = set()
        for deal in fuzzy_candidates:
            title = deal.title or ""
            best_score = 0.0
            best_alias = None
            for alias in aliases:
                score = _title_match_score(alias, title)
                if score > best_score:
                    best_score = score
                    best_alias = alias
            if best_score < 0.72 or best_alias is None:
                continue
            deal_id = str(deal.id)
            if deal_id in seen_fuzzy_ids:
                continue
            deal.document_count = deal.documents.count()
            deal.chunk_count = deal.chunks.count()
            deal.analysis_count = deal.analyses.count()
            scored_candidates.append((best_score, deal, best_alias))
            seen_fuzzy_ids.add(deal_id)

        if scored_candidates:
            scored_candidates.sort(key=lambda item: (-item[0], _deal_rank_key(item[1])))
            top_score, top_deal, top_alias = scored_candidates[0]
            second_score = scored_candidates[1][0] if len(scored_candidates) > 1 else 0.0
            if len(scored_candidates) == 1 or top_score - second_score >= 0.18:
                matches.append(top_deal)
                seen_ids.add(str(top_deal.id))
                matched_by = f"fuzzy:{top_alias}"

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
