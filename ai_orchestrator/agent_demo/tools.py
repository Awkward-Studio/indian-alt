from __future__ import annotations

import re
from typing import Any

from django.db.models import Q

from ai_orchestrator.models import DocumentChunk
from deals.models import Deal


MAX_DEAL_SUMMARY_CHARS = 700
MAX_CHUNK_CHARS = 900


def search_deals(*, query: str, limit: int = 6, **_: Any) -> dict[str, Any]:
    query = str(query or "").strip()
    limit = _safe_limit(limit, default=6, maximum=12)
    terms = _query_terms(query)

    qs = Deal.objects.all()
    if terms:
        filters = Q()
        for term in terms:
            filters |= (
                Q(title__icontains=term)
                | Q(industry__icontains=term)
                | Q(sector__icontains=term)
                | Q(deal_summary__icontains=term)
                | Q(company_details__icontains=term)
                | Q(deal_details__icontains=term)
                | Q(comments__icontains=term)
            )
        qs = qs.filter(filters)

    candidates = list(
        qs.only(
            "id",
            "title",
            "industry",
            "sector",
            "current_phase",
            "priority",
            "funding_ask",
            "funding_ask_for",
            "deal_summary",
            "company_details",
            "deal_details",
        )[: max(limit * 4, limit)]
    )
    scored = sorted(candidates, key=lambda deal: _deal_score(deal, terms), reverse=True)[:limit]
    return {
        "query": query,
        "count": len(scored),
        "deals": [_serialize_deal(deal) for deal in scored],
        "message": f"Found {len(scored)} candidate deals.",
    }


def retrieve_chunks(*, query: str, deal_ids: list[str] | None = None, limit: int = 12, **_: Any) -> dict[str, Any]:
    query = str(query or "").strip()
    terms = _query_terms(query)
    limit = _safe_limit(limit, default=12, maximum=30)
    deal_ids = [str(item) for item in (deal_ids or []) if item]

    qs = (
        DocumentChunk.objects.exclude(content="")
        .select_related("deal")
        .only("id", "deal_id", "deal__title", "source_type", "source_id", "content", "metadata", "created_at")
    )
    if deal_ids:
        qs = qs.filter(deal_id__in=deal_ids)

    if terms:
        filters = Q()
        for term in terms:
            filters |= Q(content__icontains=term)
        candidate_qs = qs.filter(filters)
    else:
        candidate_qs = qs

    candidates = list(candidate_qs.order_by("-created_at")[: max(limit * 8, limit)])
    if not candidates and deal_ids:
        candidates = list(qs.order_by("-created_at")[: max(limit * 4, limit)])

    scored = sorted(candidates, key=lambda chunk: _chunk_score(chunk, terms), reverse=True)[:limit]
    chunks = [_serialize_chunk(chunk) for chunk in scored]
    return {
        "query": query,
        "deal_ids": deal_ids,
        "count": len(chunks),
        "chunks": chunks,
        "message": f"Retrieved {len(chunks)} evidence chunks.",
    }


def verify_evidence(
    *,
    question: str,
    draft_answer: str,
    evidence: list[dict[str, Any]] | None = None,
    **_: Any,
) -> dict[str, Any]:
    draft_answer = str(draft_answer or "").strip()
    evidence = evidence or []
    supported_refs = []
    weak_flags = []

    for index, chunk in enumerate(evidence, start=1):
        text = str(chunk.get("text") or "")
        if text:
            supported_refs.append(
                {
                    "ref": chunk.get("citation") or f"chunk:{chunk.get('chunk_id') or index}",
                    "deal": chunk.get("deal"),
                    "source_title": chunk.get("source_title"),
                }
            )

    if not draft_answer:
        weak_flags.append("Draft answer is empty.")
    if not evidence:
        weak_flags.append("No retrieved evidence was supplied.")
    if draft_answer and not re.search(r"\[[^\]]+\]|\bsource\b|\bevidence\b|\bcitation\b", draft_answer, re.I):
        weak_flags.append("Draft answer does not visibly cite or refer to supporting evidence.")

    status = "supported" if supported_refs and not weak_flags else "weak"
    return {
        "question": question,
        "status": status,
        "supported_refs": supported_refs[:10],
        "weak_flags": weak_flags,
        "message": (
            f"Verification status: {status}. "
            f"{len(supported_refs)} evidence refs available; {len(weak_flags)} warnings."
        ),
    }


def final_answer(*, answer: str, citations: list[Any] | None = None, **_: Any) -> dict[str, Any]:
    return {
        "answer": str(answer or "").strip(),
        "citations": citations or [],
        "message": "Final answer produced.",
    }


def _safe_limit(value: Any, *, default: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, maximum))


def _query_terms(query: str) -> list[str]:
    stopwords = {
        "a", "an", "and", "are", "as", "at", "by", "for", "from", "how", "in",
        "is", "it", "me", "of", "on", "or", "our", "the", "to", "what", "which",
        "why", "with", "deal", "deals", "pipeline", "compare", "similar",
    }
    terms = []
    for term in re.findall(r"[A-Za-z0-9][A-Za-z0-9&.-]{2,}", query.lower()):
        if term not in stopwords and term not in terms:
            terms.append(term)
    return terms[:12]


def _deal_score(deal: Deal, terms: list[str]) -> int:
    blob = " ".join(
        str(value or "")
        for value in [
            deal.title,
            deal.industry,
            deal.sector,
            deal.deal_summary,
            deal.company_details,
            deal.deal_details,
        ]
    ).lower()
    if not terms:
        return 0
    score = 0
    for term in terms:
        score += 8 if term in str(deal.title or "").lower() else 0
        score += 4 if term in str(deal.industry or "").lower() else 0
        score += 4 if term in str(deal.sector or "").lower() else 0
        score += blob.count(term)
    return score


def _chunk_score(chunk: DocumentChunk, terms: list[str]) -> int:
    content = str(chunk.content or "").lower()
    metadata = chunk.metadata or {}
    title = str(metadata.get("title") or metadata.get("filename") or metadata.get("document_name") or "").lower()
    if not terms:
        return 0
    score = 0
    for term in terms:
        score += 5 if term in title else 0
        score += content.count(term)
    return score


def _serialize_deal(deal: Deal) -> dict[str, Any]:
    return {
        "deal_id": str(deal.id),
        "title": deal.title,
        "industry": deal.industry,
        "sector": deal.sector,
        "current_phase": deal.current_phase,
        "priority": deal.priority,
        "funding_ask": deal.funding_ask,
        "funding_ask_for": deal.funding_ask_for,
        "summary": _trim(deal.deal_summary or deal.company_details or deal.deal_details, MAX_DEAL_SUMMARY_CHARS),
    }


def _serialize_chunk(chunk: DocumentChunk) -> dict[str, Any]:
    metadata = chunk.metadata or {}
    source_title = (
        metadata.get("title")
        or metadata.get("filename")
        or metadata.get("document_name")
        or metadata.get("citation_label")
        or chunk.source_type
    )
    return {
        "chunk_id": str(chunk.id),
        "deal_id": str(chunk.deal_id) if chunk.deal_id else None,
        "deal": chunk.deal.title if chunk.deal else None,
        "source_type": chunk.source_type,
        "source_id": chunk.source_id,
        "source_title": source_title,
        "citation": f"{chunk.deal.title if chunk.deal else 'Unknown'} | {source_title}",
        "text": _trim(chunk.content, MAX_CHUNK_CHARS),
    }


def _trim(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."
