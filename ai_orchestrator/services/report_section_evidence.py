from __future__ import annotations

import hashlib
import re
from collections import Counter
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist

from ai_orchestrator.models import DocumentChunk
from ai_orchestrator.services.bulk_prompt_contracts import BULK3_SECTION_INSTRUCTIONS
from ai_orchestrator.services.embedding_processor import EmbeddingService
from ai_orchestrator.services.pipeline_registry import PipelineRegistryService
from ai_orchestrator.services.token_budget import estimate_tokens


SECTION_RETRIEVAL_TERMS = {
    "Executive Summary": "investment verdict company business model products geography raise use of funds revenue EBITDA cash burn valuation management market share funding history competitive position key risks accounting quality diligence gates",
    "Company Details": "company incorporation legal entities subsidiaries products customers sales contracts pricing discounts channels backlog returns complaints suppliers factories machinery capacity utilization production lead times scrap downtime inventory workforce intellectual property patents trademarks",
    "Promoter and Management Details": "founder promoter directors senior management roles tenure ownership ESOP compensation past ventures board independence attrition succession management references internal controls delegation audit qualifications related party transactions budget variance reporting",
    "Industry Overview": "industry demand market size TAM SAM SOM market share growth competitors substitutes price quality service innovation seasonality cyclicality imports exports supply capacity regulation entry barriers distribution customer bargaining supplier bargaining",
    "Transaction Details": "fund raise rationale prior rounds failed processes investment amount term sheet CCPS OCPS ESOP warrants convertibles cap table share classes pre-money post-money dilution use of funds debt covenants guarantees collateral investor rights",
    "Key Financials": "historical projected P&L audited statements revenue sales margins gross margin contribution margin EBITDA PAT operating cash flow free cash flow balance sheet working capital receivables aging payables inventory cash debt ROCE ROIC ROE DuPont provisions exceptional items capex forecasts budget variance tax",
    "Transaction / Trading Multiples": "transaction comparable trading comparable valuation revenue multiple EBITDA multiple market cap CAGR margins debt cash acquirer investor deal date India global peers DCF WACC terminal growth premium discount",
    "Risk Factors": "risk downside concentration customer supplier distributor promoter key person product obsolescence IP capacity downtime labor safety cash conversion earnings quality debt covenant tax litigation regulatory environmental insurance mitigant diligence",
    "Investment Rationale": "investment thesis rationale customer retention market share product reputation moat IP unit economics operating efficiency management execution cash conversion capital efficiency valuation counterevidence durability diligence condition",
    "Exit Considerations": "exit valuation entry valuation multiple dilution stake proceeds return IRR MOIC buyer IPO strategic acquisition secondary sale preference stack future capital DCF WACC exit timing sensitivity",
    "Next Steps": "diligence gap open question customer supplier management references plant inspection audited accounts cash flow reconciliation receivables aging cap table debt covenant tax litigation environmental permit insurance valuation comparable DCF action owner priority",
}


# The semantic query must retrieve material that can challenge a thesis, not
# merely passages that describe the company. Keep the original section terms
# for continuity with indexed deal documents and add evidence of the tests.
SECTION_RETRIEVAL_TESTS = {
    "Executive Summary": "investment committee recommendation decision conditions contrary evidence failed fundraise delayed spending forecast miss liquidity shortfall source conflict",
    "Company Details": "customer cohort retention lost accounts order cancellations product returns warranty claims gross margin by product factory utilization downtime scrap supplier alternatives inventory aging operating bottleneck",
    "Promoter and Management Details": "management track record budget versus actual missed milestones leadership departures founder dependence related party transactions auditor findings board minutes succession references incentive terms",
    "Industry Overview": "market size methodology comparable period geography company market share versus industry growth pricing pressure lost bids competitor wins imports substitutes excess capacity customer switching",
    "Transaction Details": "signed term sheet shareholder agreement fully diluted cap table conversion waterfall liquidation preference debt maturity cash runway sources and uses prior failed raise consent rights",
    "Key Financials": "audited versus management accounts P&L balance sheet cash flow reconciliation revenue recognition receivable aging inventory write downs EBITDA to CFO bridge forecast versus actual assumptions covenant headroom",
    "Transaction / Trading Multiples": "enterprise value equity value net debt peer selection comparable rejection matching period margin growth transaction date cycle trading range DCF sensitivity management valuation gap",
    "Risk Factors": "documented loss breach near miss leading indicator downside sensitivity mitigation effectiveness insurance exclusion owner unresolved diligence contradiction",
    "Investment Rationale": "customer evidence repeat purchase market share gain pricing power contribution margin cash conversion moat durability failed hypothesis contrary evidence valuation support",
    "Exit Considerations": "realized sector exits buyer appetite transaction multiples preference stack dilution future capital timing IPO eligibility strategic fit downside proceeds",
    "Next Steps": "unresolved contradiction missing primary record customer supplier reference request plant visit audit reconciliation legal opinion tax assessment decision gate test owner due date",
}

SECTION_RETRIEVAL_DISPLAY_DATA = {
    "Executive Summary": "historical forecast KPI scorecard period unit actual budget management projection source table",
    "Company Details": "product segment sales mix customer channel concentration facility capacity utilization supplier schedule period unit source table",
    "Promoter and Management Details": "management roster ownership option grants executive tenure budget delivery board committee related party schedule source table",
    "Industry Overview": "market size share estimate year geography methodology named competitor price capacity demand series source table",
    "Transaction Details": "term sheet sources and uses cap table fully diluted share count security rights debt maturity valuation bridge source schedule",
    "Key Financials": "audited multi-year income statement balance sheet cash flow statement actual budget forecast spreadsheet rows and cells revenue gross profit EBITDA PAT operating cash flow cash debt receivables inventory payables capex consistent currency scale period standalone consolidated basis",
    "Transaction / Trading Multiples": "peer trading multiple transaction precedent dated valuation enterprise equity value net debt revenue EBITDA growth margin period source table",
    "Risk Factors": "risk register loss history exposure magnitude covenant headroom insurance limit concentration sensitivity scenario source schedule",
    "Investment Rationale": "thesis proof repeat customer economics segment margin cohort retention counterevidence forecast assumption source series",
    "Exit Considerations": "entry and exit capitalization preference waterfall dilution proceeds multiple MOIC IRR timing sensitivity source model",
    "Next Steps": "open diligence issue named missing record discrepancy test threshold owner priority dependency source evidence",
}


def build_section_retrieval_template(title: str) -> str:
    """Build an editable semantic query for the section's underwriting evidence."""
    guidance = BULK3_SECTION_INSTRUCTIONS[title]
    terms = SECTION_RETRIEVAL_TERMS[title]
    tests = SECTION_RETRIEVAL_TESTS[title]
    display_data = SECTION_RETRIEVAL_DISPLAY_DATA[title]
    return (
        f"{{{{ deal_title }}}}. Internal investment committee report section: "
        f"{{{{ section_title }}}}. {guidance} Relevant evidence: {terms}. "
        f"Evidence that tests or challenges the investment case: {tests}. "
        f"Source tables and comparable data for the report: {display_data}. "
        "Find primary records, historical comparisons, management assumptions, "
        "independent corroboration, contrary facts, missing inputs and source conflicts. "
        "Preserve exact values, periods, units, actual versus forecast status, "
        "source names and locations, and the evidence needed to explain why a "
        "finding changes the investment decision."
    )


def build_industry_deal_comparison_template() -> str:
    """Semantic query for the Industry Overview's internal peer-evidence step."""
    return (
        "{{ deal_title }}. {{ section_title }}. Find source-backed companies in our deal "
        "database with similar products, buyers, use cases, distribution or business "
        "models. Rank substantive passages about those overlaps and differences, "
        "pricing, capacity, customer retention, growth, margins, market position and "
        "competitive wins or losses. Search across indexed deal documents without "
        "an industry filter. Prefer comparable periods, units and accounting bases; "
        "include contrary evidence and precise source locations. Semantic similarity "
        "alone makes a company a peer candidate, not a confirmed competitor."
    )


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
            parsed = urlsplit(url)
            if parsed.scheme.lower() not in {"http", "https"}:
                return ""
            if (parsed.hostname or "").lower() in {"example.com", "example.org", "example.net"}:
                return ""
            return url
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
    def _location_details(metadata: dict) -> dict:
        details = {
            "kind": str(metadata.get("chunk_kind") or "document_text"),
            "source_location": metadata.get("source_location"),
            "section": metadata.get("section"),
            "sheet_name": metadata.get("sheet_name"),
            "page": metadata.get("page"),
            "slide": metadata.get("slide"),
            "row_start": metadata.get("row_start"),
            "row_end": metadata.get("row_end"),
            "column_start": metadata.get("column_start"),
            "column_end": metadata.get("column_end"),
            "cell_range": metadata.get("cell_range"),
        }
        return {key: value for key, value in details.items() if value not in (None, "")}

    @classmethod
    def _location(cls, metadata: dict) -> str:
        details = cls._location_details(metadata)
        sheet_name = str(details.get("sheet_name") or "").strip()
        cell_range = str(details.get("cell_range") or "").strip()
        if not cell_range and details.get("row_start") not in (None, ""):
            row_start = details["row_start"]
            row_end = details.get("row_end") or row_start
            column_start = str(details.get("column_start") or "A")
            column_end = str(details.get("column_end") or column_start)
            cell_range = f"{column_start}{row_start}:{column_end}{row_end}"
        if sheet_name and cell_range:
            return f"{sheet_name}!{cell_range}"
        if sheet_name:
            return f"sheet {sheet_name}"
        if details.get("page") not in (None, ""):
            return f"p. {details['page']}"
        if details.get("slide") not in (None, ""):
            return f"slide {details['slide']}"
        if details.get("section") not in (None, ""):
            return f"section {details['section']}"
        source_location = details.get("source_location")
        if isinstance(source_location, dict):
            return ", ".join(
                f"{item_key} {item_value}"
                for item_key, item_value in source_location.items()
                if item_value not in (None, "")
            )
        return str(source_location or "").strip()

    def _citation(self, chunk: DocumentChunk, *, rank: int) -> dict:
        metadata = chunk.metadata or {}
        source_id = str(chunk.source_id)
        title = str(self.document_titles.get(source_id) or metadata.get("title") or source_id).strip()
        location = self._location(metadata)
        label_title = title.replace("[", "\\[").replace("]", "\\]")
        label = f"{label_title}, {location}" if location else label_title
        url = self.document_urls.get(source_id) or ""
        return {
            "rank": rank,
            "document_id": source_id,
            "title": title,
            "url": url,
            "location": location,
            "locator": self._location_details(metadata),
            "inline": f"[{label}](<{url}>)" if url else label,
            "reference": f"[{label_title}](<{url}>)" if url else label_title,
        }

    def _query(self, title: str) -> str:
        from ai_orchestrator.services.bulk_prompt_contracts import IC_REPORT_SECTION_STAGE_KEYS

        stage_key = f"retrieval_{IC_REPORT_SECTION_STAGE_KEYS[title]}"
        try:
            _, query, _ = PipelineRegistryService.render_prompt_stage(
                "ic_report_generation",
                stage_key,
                deal_title=self.deal.title,
                section_title=title,
            )
        except ObjectDoesNotExist:
            PipelineRegistryService.ensure_report_pipeline_defaults()
            _, query, _ = PipelineRegistryService.render_prompt_stage(
                "ic_report_generation",
                stage_key,
                deal_title=self.deal.title,
                section_title=title,
            )
        return query

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

    def _select(self, candidates: list[DocumentChunk], *, token_budget: int | None = None) -> list[DocumentChunk]:
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
        budget = int(token_budget or self.max_tokens)

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
            if selected and used_tokens + block_tokens > budget:
                return False
            if not selected and block_tokens > budget:
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
            f"Citation marker: [R{rank:03d}]",
            f"Document: {citation['title']}",
            f"Evidence type: {metadata.get('chunk_kind') or 'document text'}",
        ]
        if citation["location"]:
            header.append(f"Location: {citation['location']}")
        if citation["locator"].get("sheet_name") and citation["locator"].get("cell_range"):
            header.append(
                "For a narrower spreadsheet citation, cite only cells visible in this block as "
                f"[R{rank:03d}@'{citation['locator']['sheet_name']}'!A1:B2]"
            )
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

        comparison = None
        if title == "Industry Overview":
            from ai_orchestrator.services.industry_deal_comparison import IndustryDealComparisonService

            try:
                _, comparison_query, _ = PipelineRegistryService.render_prompt_stage(
                    "ic_report_generation",
                    "industry_our_deal_comparison",
                    deal_title=self.deal.title,
                    section_title=title,
                )
            except ObjectDoesNotExist:
                PipelineRegistryService.ensure_report_pipeline_defaults()
                _, comparison_query, _ = PipelineRegistryService.render_prompt_stage(
                    "ic_report_generation",
                    "industry_our_deal_comparison",
                    deal_title=self.deal.title,
                    section_title=title,
                )
            comparison = IndustryDealComparisonService(
                deal=self.deal, embedding_service=self.embedding_service
            ).retrieve(comparison_query)

        comparison_budget = min(16_000, int(self.max_tokens * 0.45)) if comparison and comparison["chunks"] else 0
        selected = self._select(candidates, token_budget=self.max_tokens - comparison_budget)
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
        comparison_selected = []
        if comparison_budget:
            source_info = comparison["source_info"]
            for source_id, info in source_info.items():
                self.document_titles[source_id] = info["title"]
                self.document_urls[source_id] = self._safe_http_url(info["url"])
            remaining = self.max_tokens - estimate_tokens(context) - 50
            company_counts: Counter[str] = Counter()
            seen = {self._identity(chunk) for chunk in selected}
            ranked = comparison["chunks"]
            # Preserve semantic rank and cap any one company's share of the pack.
            for chunk in ranked:
                info = source_info[str(chunk.source_id)]
                company = info["company"]
                identity = self._identity(chunk)
                if identity in seen or company_counts[company] >= 6 or len(comparison_selected) >= 48:
                    continue
                rank = len(selected) + len(comparison_selected) + 1
                block = (
                    f"Peer company: {company} | Database relationship: {info['relationship']}\n"
                    + self._format_chunk(chunk, rank=rank)
                )
                cost = estimate_tokens(block) + 2
                if cost > remaining:
                    continue
                comparison_selected.append((chunk, block))
                citations[str(rank)] = self._citation(chunk, rank=rank)
                company_counts[company] += 1
                seen.add(identity)
                remaining -= cost
            if comparison_selected:
                context += (
                    "\n\nInternal database comparison evidence. A semantic peer candidate "
                    "is not a confirmed direct competitor. Keep its company and source separate "
                    "from the target deal.\n\n"
                    + "\n\n".join(block for _, block in comparison_selected)
                )
        source_counts = Counter(str(chunk.source_id) for chunk in selected)
        stats = {
            "strategy": strategy,
            "candidate_count": len(candidates),
            "selected_chunk_count": len(selected) + len(comparison_selected),
            "selected_document_count": len(source_counts),
            "selected_document_ids": list(source_counts),
            "supplemented_document_ids": supplemented_document_ids,
            "estimated_context_tokens": estimate_tokens(context),
            "context_budget_tokens": self.max_tokens,
        }
        if comparison is not None:
            stats["our_deal_comparison"] = {
                "retrieval_scope": "global_indexed_deal_documents",
                "candidate_deal_count": comparison["candidate_deal_count"],
                "ranked_chunk_count": len(comparison["chunks"]),
                "selected_chunk_count": len(comparison_selected),
                "context_budget_tokens": comparison_budget,
                "estimated_context_tokens": sum(
                    estimate_tokens(block) + 2 for _, block in comparison_selected
                ),
                "selected_companies": sorted({
                    comparison["source_info"][str(chunk.source_id)]["company"]
                    for chunk, _ in comparison_selected
                }),
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
