"""Retrieve fresh, deal-linked evidence for report rewrites."""
import re
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from django.db.models.functions import Coalesce

from ai_orchestrator.models import DocumentChunk
from ai_orchestrator.services.embedding_processor import EmbeddingService
from ai_orchestrator.services.report_competitor_evidence import saved_competitor_blocks
from ai_orchestrator.services.token_budget import estimate_message_tokens



@dataclass
class RewriteEvidence:
    kind: str
    source_id: str
    title: str
    content: str
    location: str
    url: str
    metadata: dict
    available_at: str = ""
    latest: bool = False


def evidence_timestamp(value):
    if not value:
        return ""
    try:
        date = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except ValueError:
        return ""
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    return date.astimezone(timezone.utc).isoformat()


def _fit_excerpt(text, token_limit):
    if estimate_message_tokens(text) <= token_limit:
        return text
    low, high = 1, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_message_tokens(text[:middle] + "…") <= token_limit:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip() + "…"


def _select_rewrite_evidence(candidates, ranked, max_tokens):
    latest_by_kind = {}
    for item in candidates:
        current = latest_by_kind.get(item.kind)
        if current is None or item.available_at > current.available_at:
            latest_by_kind[item.kind] = item

    # For the same recent source, prefer its most relevant passage.
    for kind, newest in latest_by_kind.items():
        latest_by_kind[kind] = next(
            (item for item in ranked if item.kind == kind and item.source_id == newest.source_id
             and item.available_at == newest.available_at),
            newest,
        )

    selected, counts, seen = [], Counter(), set()
    remaining = max_tokens

    def add(item, *, latest=False, excerpt_limit=None):
        nonlocal remaining
        key = (item.kind, item.source_id, item.metadata["chunk_index"])
        if key in seen or counts[item.kind] >= 5:
            return
        overhead = 120 + estimate_message_tokens(item.title + item.location)
        if excerpt_limit is not None:
            excerpt_limit = min(remaining - overhead, excerpt_limit - overhead)
            if excerpt_limit < 4:
                return
        text = _fit_excerpt(item.content, excerpt_limit) if excerpt_limit is not None else item.content
        cost = estimate_message_tokens(text) + overhead
        if cost <= remaining:
            selected.append(replace(item, content=text, latest=latest))
            counts[item.kind] += 1
            seen.add(key)
            remaining -= cost

    if latest_by_kind:
        per_source = (max_tokens // 2) // len(latest_by_kind)
        for item in sorted(latest_by_kind.values(), key=lambda candidate: candidate.available_at, reverse=True):
            add(item, latest=True, excerpt_limit=per_source)

    for item in ranked:
        add(item)
    return selected

def ranked_rewrite_evidence(deal, query, *, max_tokens=5000):
    """Rank complementary evidence while reserving room for each available source type."""
    candidates = []
    seen = set()

    def add(kind, source_id, title, text, *, location="", url="", chunk_index=0, available_at=None):
        text = (text or "").strip()[:4800]
        identity = (kind, str(source_id), chunk_index)
        if not text or identity in seen:
            return
        seen.add(identity)
        candidates.append(RewriteEvidence(
            kind=kind, source_id=str(source_id), title=str(title)[:240], content=text,
            location=str(location)[:240], url=url, available_at=evidence_timestamp(available_at),
            metadata={"chunk_kind": kind, "chunk_index": chunk_index},
        ))

    service = EmbeddingService()
    emails = deal.emails.annotate(evidence_date=Coalesce("date_received", "created_at"))
    email_dates = {str(pk): date for pk, date in emails.values_list("id", "evidence_date")}
    documents = deal.documents.annotate(evidence_date=Coalesce("last_chunked_at", "created_at"))
    document_dates = {str(pk): date for pk, date in documents.values_list("id", "evidence_date")}
    news = documents.filter(title__istartswith="Public Domain News Research")
    news_ids = {str(pk) for pk in news.values_list("id", flat=True)}
    indexed_ids = {str(pk) for pk in documents.filter(is_indexed=True).values_list("id", flat=True)}

    for kind, source_type, dates in (
        ("Email", "email", email_dates),
        ("Company news", "document", {pk: document_dates[pk] for pk in news_ids & indexed_ids}),
        ("Deal document", "document", {pk: document_dates[pk] for pk in indexed_ids - news_ids}),
    ):
        if not dates:
            continue
        newest_ids = sorted(dates, key=dates.__getitem__, reverse=True)[:12]
        deal_chunks = DocumentChunk.objects.filter(deal=deal, source_type=source_type)
        latest_id = newest_ids[0]
        chunks = [
            *deal_chunks.filter(source_id=latest_id).order_by("-created_at", "id")[:2],
            *deal_chunks.filter(source_id__in=newest_ids).order_by("-created_at", "id")[:12],
        ]
        try:
            matches = service.search_global_chunks(
                query, limit=16, deal_ids=[str(deal.id)], source_ids=list(dates), rerank=False,
            )
        except Exception:
            matches = []
        for chunk in [*chunks, *matches]:
            if chunk.source_type != source_type or str(chunk.source_id) not in dates:
                continue
            metadata = chunk.metadata or {}
            date = dates[str(chunk.source_id)]
            if source_type == "document":
                date = max(date, chunk.indexed_at or chunk.created_at or date)
            add(kind, chunk.source_id, metadata.get("title") or metadata.get("subject") or kind,
                chunk.content, location=date.date().isoformat(), available_at=date,
                chunk_index=int(metadata.get("chunk_index") or 0))

    for email in emails.order_by("-evidence_date", "-created_at")[:12]:
        add("Email", email.pk, email.subject or "Linked email", email.body_text or email.body_preview or email.extracted_text,
            location=email.evidence_date.date().isoformat(), available_at=email.evidence_date, chunk_index=-1)
    for kind, queryset in (("Company news", news), ("Deal document", documents.exclude(pk__in=news_ids))):
        for document in queryset.order_by("-evidence_date", "-created_at")[:12]:
            add(kind, document.pk, document.title or kind, document.normalized_text or document.extracted_text,
                location=document.evidence_date.date().isoformat(), available_at=document.evidence_date, chunk_index=-1)
    meetings = deal.meeting_notes.filter(is_indexed=True).annotate(evidence_date=Coalesce("meeting_at", "created_at"))
    for meeting in meetings.order_by("-evidence_date", "-created_at")[:12]:
        add("Meeting", meeting.pk, meeting.title or "Meeting note", meeting.summary or meeting.body,
            location=meeting.evidence_date.date().isoformat(), available_at=meeting.evidence_date)
    for index, item in enumerate(saved_competitor_blocks(deal, None, max_tokens=12_000)):
        add("Competitor", item["source_id"], item["title"], item["text"],
            location=item["location"], url=item["url"], chunk_index=index, available_at=item.get("available_at"))

    if not candidates:
        return []
    terms = set(re.findall(r"\b\w{4,}\b", query.casefold()))
    lexical = sorted(candidates, key=lambda item: -len(terms & set(re.findall(
        r"\b\w{4,}\b", f"{item.title} {item.content}".casefold(),
    ))))
    ranked = service._model_rerank_chunks(candidates, query=query, limit=len(candidates)) if service.reranker_model else []
    ranked = [*ranked, *(item for item in lexical if item not in ranked)]

    return _select_rewrite_evidence(candidates, ranked, max_tokens)
