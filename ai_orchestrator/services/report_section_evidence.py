from __future__ import annotations

import hashlib
import re
from collections import Counter
from urllib.parse import urlsplit

from django.conf import settings

from ai_orchestrator.models import DocumentChunk
from ai_orchestrator.services.bulk_prompt_contracts import BULK3_SECTION_INSTRUCTIONS
from ai_orchestrator.services.embedding_processor import EmbeddingService
from ai_orchestrator.services.token_budget import estimate_tokens


SECTION_RETRIEVAL_TERMS = {
    "Executive Summary": "investment verdict strongest facts key metrics principal risks diligence priorities",
    "Company Details": "company products services business model revenue sources customers investors highlights concerns",
    "Promoter and Management Details": "founder promoter management leadership experience designation education ownership red flags",
    "Industry Overview": "industry demand market size TAM growth competition competitors moat value chain supply constraints",
    "Transaction Details": "fund raise investment amount instrument valuation ownership round leader follow-on funds raised sourcing",
    "Key Financials": "historical projected P&L revenue sales margins gross margin contribution margin EBITDA expenses balance sheet working capital receivables payables inventory cash debt ROCE ROIC ROE",
    "Transaction / Trading Multiples": "transaction comparable trading comparable valuation revenue multiple EBITDA multiple market cap CAGR margins debt cash acquirer investor deal date",
    "Risk Factors": "risk downside concern dependency concentration churn margin pressure cash burn debt regulation execution mitigant diligence",
    "Investment Rationale": "investment thesis rationale growth unit economics moat returns quality scalability evidence concern",
    "Exit Considerations": "exit valuation entry valuation multiple dilution stake return IRR MOIC buyer IPO strategic acquisition assumptions",
    "Next Steps": "diligence gap open question verify validation action owner task next step missing evidence",
}


class ICReportSectionEvidenceService:
    """Build a distinct, bounded evidence pack for each IC report section."""

    def __init__(
        self,
        *,
        deal,
        documents,
        embedding_service=None,
        candidate_limit: int | None = None,
        max_chunks: int | None = None,
        max_tokens: int | None = None,
        min_chunks_per_document: int | None = None,
    ):
        self.deal = deal
        self.documents = list(documents)
        self.embedding_service = embedding_service or EmbeddingService()
        self.candidate_limit = max(
            1,
            int(candidate_limit or getattr(settings, "VDR_REPORT_SECTION_CANDIDATES", 320)),
        )
        self.max_chunks = max(
            1,
            int(max_chunks or getattr(settings, "VDR_REPORT_SECTION_MAX_CHUNKS", 240)),
        )
        self.max_tokens = max(
            4_000,
            int(max_tokens or getattr(settings, "VDR_REPORT_SECTION_EVIDENCE_TOKENS", 36_000)),
        )
        self.min_chunks_per_document = max(
            1,
            int(
                min_chunks_per_document
                or getattr(settings, "VDR_REPORT_SECTION_MIN_CHUNKS_PER_DOCUMENT", 8)
            ),
        )
        self.source_ids = [str(document.id) for document in self.documents]
        self.document_titles = {str(document.id): document.title for document in self.documents}
        self.document_urls = {
            str(document.id): self._document_source_url(document)
            for document in self.documents
        }
        self.section_stats: dict[str, dict] = {}

    @staticmethod
    def _safe_http_url(value) -> str:
        url = str(value or "").strip()
        try:
            return url if urlsplit(url).scheme.lower() in {"http", "https"} else ""
        except ValueError:
            return ""

    @classmethod
    def _document_source_url(cls, document) -> str:
        direct = cls._safe_http_url(getattr(document, "file_url", None))
        if direct:
            return direct
        evidence = getattr(document, "evidence_json", None)
        source_metadata = evidence.get("source_metadata", {}) if isinstance(evidence, dict) else {}
        return cls._safe_http_url(
            source_metadata.get("source_url") or source_metadata.get("webUrl")
        )

    @staticmethod
    def _location(metadata: dict) -> str:
        candidates = (
            ("source_location", ""),
            ("section", "section "),
            ("sheet_name", "sheet "),
            ("page", "p. "),
            ("slide", "slide "),
        )
        for key, prefix in candidates:
            value = metadata.get(key)
            if value in (None, ""):
                continue
            if isinstance(value, dict):
                rendered = ", ".join(
                    f"{item_key} {item_value}"
                    for item_key, item_value in value.items()
                    if item_value not in (None, "")
                )
                return rendered
            return f"{prefix}{value}"
        return ""

    def _citation(self, chunk: DocumentChunk, *, rank: int) -> dict:
        metadata = chunk.metadata or {}
        source_id = str(chunk.source_id)
        title = str(self.document_titles.get(source_id) or metadata.get("title") or source_id).strip()
        location = self._location(metadata)
        year_match = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", title)
        year = year_match.group(1) if year_match else "n.d."
        label_title = title.replace("[", "\\[").replace("]", "\\]")
        detail = f"{label_title}. ({year}). Internal company document"
        if location:
            detail += f", {location}"
        detail += "."
        reference = f"{label_title}. ({year}). Internal company document."
        url = self.document_urls.get(source_id) or ""
        return {
            "rank": rank,
            "document_id": source_id,
            "title": title,
            "url": url,
            "location": location,
            "inline": f"[{detail}](<{url}>)" if url else detail,
            "reference": f"[{reference}](<{url}>)" if url else reference,
        }

    def _query(self, title: str) -> str:
        guidance = BULK3_SECTION_INSTRUCTIONS.get(title, "")
        terms = SECTION_RETRIEVAL_TERMS.get(title, "")
        return (
            f"{self.deal.title}. Internal investment committee report section: {title}. "
            f"{guidance} Relevant evidence: {terms}. Preserve exact values, periods, units, "
            "assumptions, conflicts, risks, source names, and missing information."
        )

    def _fallback_chunks(self) -> list[DocumentChunk]:
        if not self.source_ids:
            return []
        return list(
            DocumentChunk.objects.filter(
                deal=self.deal,
                source_type="document",
                source_id__in=self.source_ids,
            )
            .exclude(content="")
            .order_by("source_id", "created_at")[: self.candidate_limit]
        )

    @staticmethod
    def _identity(chunk: DocumentChunk) -> str:
        normalized = " ".join(str(chunk.content or "").split()).casefold()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def _select(self, candidates: list[DocumentChunk]) -> list[DocumentChunk]:
        eligible = [
            chunk
            for chunk in candidates
            if chunk.source_type == "document" and str(chunk.source_id) in self.document_titles
        ]
        if not eligible:
            return []

        # Keep any single spreadsheet from swallowing the complete context while
        # still allowing it to dominate a financial section when it ranks well.
        per_document_cap = max(4, int(self.max_chunks * 0.70))
        selected: list[DocumentChunk] = []
        source_counts: Counter[str] = Counter()
        seen_content: set[str] = set()
        used_tokens = 0

        def add(chunk: DocumentChunk) -> bool:
            nonlocal used_tokens
            if len(selected) >= self.max_chunks:
                return False
            source_id = str(chunk.source_id)
            identity = self._identity(chunk)
            if identity in seen_content or source_counts[source_id] >= per_document_cap:
                return False
            block = self._format_chunk(chunk, rank=len(selected) + 1)
            block_tokens = estimate_tokens(block) + 2
            if selected and used_tokens + block_tokens > self.max_tokens:
                return False
            if not selected and block_tokens > self.max_tokens:
                return False
            selected.append(chunk)
            seen_content.add(identity)
            source_counts[source_id] += 1
            used_tokens += block_tokens
            return True

        by_source = {
            source_id: [chunk for chunk in eligible if str(chunk.source_id) == source_id]
            for source_id in self.source_ids
        }
        for offset in range(self.min_chunks_per_document):
            for source_id in self.source_ids:
                source_candidates = by_source.get(source_id) or []
                if offset < len(source_candidates):
                    add(source_candidates[offset])

        for chunk in eligible:
            add(chunk)
            if len(selected) >= self.max_chunks:
                break
        return selected

    def _format_chunk(self, chunk: DocumentChunk, *, rank: int) -> str:
        metadata = chunk.metadata or {}
        citation = self._citation(chunk, rank=rank)
        header = [
            f"Retrieval block R{rank:03d} (internal ordering only; never cite this label)",
            f"Required citation: {citation['inline']}",
            f"Document: {citation['title']}",
            f"Evidence type: {metadata.get('chunk_kind') or 'document text'}",
        ]
        if citation["location"]:
            header.append(f"Location: {citation['location']}")
        return " | ".join(header) + "\n" + str(chunk.content or "").strip()

    def retrieve(self, title: str) -> dict:
        query = self._query(title)
        strategy = "hybrid_pgvector_fts"
        try:
            candidates = self.embedding_service.search_global_chunks(
                query,
                limit=self.candidate_limit,
                deal_ids=[str(self.deal.id)],
                source_ids=self.source_ids,
                rerank=False,
            )
        except Exception:
            candidates = []
        present_source_ids = {
            str(chunk.source_id)
            for chunk in candidates
            if chunk.source_type == "document"
        }
        supplemented_document_ids = []
        for source_id in self.source_ids:
            if source_id in present_source_ids:
                continue
            try:
                supplement = self.embedding_service.search_global_chunks(
                    query,
                    limit=max(self.min_chunks_per_document * 3, 24),
                    deal_ids=[str(self.deal.id)],
                    source_ids=[source_id],
                    rerank=False,
                )
            except Exception:
                supplement = []
            if supplement:
                candidates.extend(supplement)
                supplemented_document_ids.append(source_id)
        if not candidates:
            strategy = "ordered_index_fallback"
            candidates = self._fallback_chunks()

        selected = self._select(candidates)
        if not selected:
            raise ValueError(f"No indexed document chunks were available for report section '{title}'.")
        context = "\n\n".join(
            self._format_chunk(chunk, rank=index)
            for index, chunk in enumerate(selected, start=1)
        )
        citations = {
            str(index): self._citation(chunk, rank=index)
            for index, chunk in enumerate(selected, start=1)
        }
        source_counts = Counter(str(chunk.source_id) for chunk in selected)
        stats = {
            "strategy": strategy,
            "candidate_count": len(candidates),
            "selected_chunk_count": len(selected),
            "selected_document_count": len(source_counts),
            "selected_document_ids": list(source_counts),
            "supplemented_document_ids": supplemented_document_ids,
            "estimated_context_tokens": estimate_tokens(context),
            "context_budget_tokens": self.max_tokens,
        }
        self.section_stats[title] = stats
        return {"context": context, "metadata": stats, "citations": citations}

    @property
    def covered_document_ids(self) -> set[str]:
        covered: set[str] = set()
        for stats in self.section_stats.values():
            covered.update(stats.get("selected_document_ids") or [])
        return covered

    @property
    def total_selected_chunks(self) -> int:
        return sum(int(stats.get("selected_chunk_count") or 0) for stats in self.section_stats.values())
