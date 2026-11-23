"""Retrieve fresh, deal-linked evidence for report rewrites."""
import re
from collections import Counter
from types import SimpleNamespace

from ai_orchestrator.models import DocumentChunk
from ai_orchestrator.services.embedding_processor import EmbeddingService
from ai_orchestrator.services.report_competitor_evidence import saved_competitor_blocks
from ai_orchestrator.services.token_budget import estimate_message_tokens


def ranked_rewrite_evidence(deal, query, *, max_tokens=5000):
    """Rank complementary evidence while reserving room for each available source type."""
    candidates = []
    seen = set()

    def add(kind, source_id, title, text, *, location="", url="", chunk_index=0):
        text = (text or "").strip()[:4800]
        identity = (kind, str(source_id), chunk_index)
        if not text or identity in seen:
            return
        seen.add(identity)
        candidates.append(SimpleNamespace(
            kind=kind, source_id=str(source_id), title=title, content=text,
            location=location, url=url,
            metadata={"chunk_kind": kind, "chunk_index": chunk_index},
        ))

    service = EmbeddingService()
    emails = deal.emails.all()
    email_ids = [str(pk) for pk in emails.values_list("id", flat=True)]
    document_ids = [str(pk) for pk in deal.documents.filter(is_indexed=True).values_list("id", flat=True)]
    news_ids = set(str(pk) for pk in deal.documents.filter(
        title__istartswith="Public Domain News Research",
    ).values_list("id", flat=True))
    for kind, source_type, ids in (
        ("Email", "email", email_ids),
        ("Company news", "document", [pk for pk in document_ids if pk in news_ids]),
        ("Deal document", "document", [pk for pk in document_ids if pk not in news_ids]),
    ):
        if not ids:
            continue
        chunks = list(DocumentChunk.objects.filter(deal=deal, source_type=source_type, source_id__in=ids)
                      .order_by("-created_at", "id")[:12])
        try:
            matches = service.search_global_chunks(
                query, limit=16, deal_ids=[str(deal.id)], source_ids=ids, rerank=False,
            )
        except Exception:
            matches = []
        for chunk in [*chunks, *matches]:
            if chunk.source_type == source_type and str(chunk.source_id) in ids:
                metadata = chunk.metadata or {}
                add(kind, chunk.source_id, metadata.get("title") or metadata.get("subject") or kind,
                    chunk.content, location=str(chunk.created_at.date()) if chunk.created_at else "",
                    chunk_index=int(metadata.get("chunk_index") or 0))

    for email in emails.order_by("-date_received", "-created_at")[:12]:
        add("Email", email.pk, email.subject or "Linked email", email.body_text or email.body_preview or email.extracted_text,
            location=str(email.date_received.date()) if email.date_received else "", chunk_index=-1)
    for document in deal.documents.filter(is_indexed=False).order_by("-created_at")[:12]:
        kind = "Company news" if str(document.pk) in news_ids else "Deal document"
        add(kind, document.pk, document.title or kind, document.normalized_text or document.extracted_text)
    for index, item in enumerate(saved_competitor_blocks(deal, None, max_tokens=12_000)):
        add("Competitor", item["source_id"], item["title"], item["text"],
            location=item["location"], url=item["url"], chunk_index=index)

    if not candidates:
        return []
    terms = set(re.findall(r"\b\w{4,}\b", query.casefold()))
    lexical = sorted(candidates, key=lambda item: -len(terms & set(re.findall(
        r"\b\w{4,}\b", f"{item.title} {item.content}".casefold(),
    ))))
    ranked = service._model_rerank_chunks(candidates, query=query, limit=len(candidates)) if service.reranker_model else []
    ranked = [*ranked, *(item for item in lexical if item not in ranked)]

    selected = []
    counts = Counter()
    remaining = max_tokens
    # Preserve source variety before filling the budget in rank order.
    first_by_source = {}
    for item in ranked:
        first_by_source.setdefault(item.kind, item)
    for item in [*first_by_source.values(), *ranked]:
        if item in selected or counts[item.kind] >= 5:
            continue
        cost = estimate_message_tokens(item.content) + 120
        if cost <= remaining:
            selected.append(item)
            counts[item.kind] += 1
            remaining -= cost
    return selected
