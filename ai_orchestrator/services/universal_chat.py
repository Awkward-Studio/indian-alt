import json
import logging
import os
import re
import resource
import sys
from difflib import SequenceMatcher
from typing import Any, Dict, List, Tuple

from django.db import connection
from django.db.models import Count, Q

from deals.models import Deal, DealDocument, FolderAnalysisDocument
from ..models import DocumentChunk
from .ai_processor import AIProcessorService
from .embedding_processor import EmbeddingService
from .flow_config import UniversalChatFlowService
from .parsers import ResponseParserService
from .prompts import PromptBuilderService
from .runtime import AIRuntimeService

logger = logging.getLogger(__name__)

HARD_MAX_DEAL_LIMIT = 20
HARD_MAX_CHUNKS_PER_DEAL = 12
HARD_MAX_GLOBAL_CHUNKS = 60
HARD_MAX_SEMANTIC_QUERIES = 4
HARD_MAX_VECTOR_CANDIDATES = 120
HARD_MAX_FALLBACK_CANDIDATES = 160
HARD_MAX_RERANK_CANDIDATES = 96
HARD_MAX_SYNTHESIS_CANDIDATES_PER_DEAL = 72
HARD_MAX_CONTEXT_CHARS = 120000


QUERY_TYPES = {
    "exact_lookup",
    "comparison",
    "stats",
    "pipeline_search",
    "timeline",
    "narrative",
}

ENTITY_TYPES = {"deal", "theme", "metric", "document"}
EVIDENCE_PREFERENCES = {"summary", "metrics", "risks", "mixed", "documents", "timeline"}
RESULT_SHAPES = {"single_deal", "named_set", "shortlist", "cross_pipeline"}
SELECTION_MODES = {"depth_first", "balanced", "breadth_first"}
STATS_MODES = {"none", "count", "group", "aggregate"}

METRIC_TOKENS = {
    "arr", "mrr", "revenue", "ebitda", "cm1", "cm2", "gross margin",
    "gmv", "cac", "ltv", "burn", "runway", "valuation", "ticket size",
    "funding ask", "irr", "moic", "arpu", "aov",
}

STAGE_TOKENS = {
    "origination", "screening", "ic", "investment committee", "due diligence",
    "term sheet", "closed", "rejected", "management meeting",
}

FOLLOW_UP_SKIP_PATTERNS = [
    r"^\s*why\??\s*$",
    r"^\s*how\??\s*$",
    r"^\s*what do you mean\??\s*$",
    r"^\s*explain( that| this| more)?\b",
    r"^\s*clarify\b",
    r"^\s*expand\b",
    r"^\s*summar(?:ize|ise)\b",
    r"^\s*rewrite\b",
    r"^\s*rephrase\b",
    r"^\s*shorten\b",
    r"^\s*make (it|this) shorter\b",
    r"^\s*turn (that|this|it) into\b",
    r"^\s*convert (that|this|it) into\b",
]

FORCE_RETRIEVAL_TERMS = {
    "find", "search", "show me", "which deals", "list deals", "similar deals",
    "compare", "comparison", "versus", "vs", "difference", "latest", "current",
    "pipeline", "count", "how many", "total", "metric", "metrics", "arr", "mrr",
    "revenue", "ebitda", "valuation", "funding ask", "stage", "phase", "status",
    "verify", "check the document", "source", "citation", "citations",
}

QUERY_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "about", "across", "all", "any",
    "be", "by", "do", "does", "for", "from", "give", "find", "list", "main",
    "me", "mention", "of", "our", "show", "strong", "tell", "the", "their",
    "these", "this", "those", "to", "top", "what", "which", "with", "you",
    "deal", "deals", "business", "company", "companies", "industry", "pipeline",
}

PLACEHOLDER_PLAN_VALUES = {
    "the company name",
    "named deal",
    "exact company names or phrases",
    "exact company names or phrases kept only for compatibility",
    "non-company phrases only",
    "semantic search query variants",
    "important keywords",
    "semantic preferences that should not become db filters",
    "thematic preferences",
    "arman financial services",
    "summary|metrics|risks|mixed|documents|timeline",
    "single_deal|named_set|shortlist|cross_pipeline",
    "depth_first|balanced|breadth_first",
    "none|count|group|aggregate",
}

GENERIC_COLLECTION_TERMS = {
    "company", "companies", "deal", "deals", "business", "businesses",
    "sector", "sectors", "industry", "industries", "pipeline", "system",
    "portfolio", "food", "foods", "consumer", "consumers", "beverage",
    "beverages", "fmcg", "microfinance", "fintech",
}

def compute_candidate_deals_for_plan(service: "UniversalChatService", plan: Dict[str, Any]) -> List[Deal]:
    return service._compute_candidate_deals(plan)


class UniversalChatService:
    """
    Planner-driven global retrieval pipeline for universal chat.
    """

    def __init__(self, ai_service: AIProcessorService, flow_config: Dict[str, Any] | None = None, flow_version: Any = None):
        self.ai_service = ai_service
        self.embed_service = EmbeddingService()
        self.is_sqlite = connection.vendor == "sqlite"
        self._document_cache: dict[str, Any] = {}
        if flow_config is not None:
            self.flow_config = UniversalChatFlowService.validate_config(flow_config)
            self.flow_version = flow_version
        else:
            self.flow_config, self.flow_version = UniversalChatFlowService.get_runtime_config()
        self.disable_hard_caps = str(os.environ.get("UNIVERSAL_CHAT_DISABLE_HARD_CAPS", "")).lower() in {
            "1",
            "true",
            "yes",
        }

    def _cap(self, value: int, hard_max: int) -> int:
        return int(value) if self.disable_hard_caps else min(int(value), hard_max)

    def _min_with_optional_hard_cap(self, values: List[int], hard_max: int) -> int:
        bounded_values = [int(value) for value in values]
        if not self.disable_hard_caps:
            bounded_values.append(hard_max)
        return min(bounded_values)

    def _trace_chunks(self, label: str, **extra: Any) -> None:
        if not os.environ.get("UNIVERSAL_CHAT_TRACE_CHUNKS"):
            return
        vmrss_kb = None
        try:
            with open("/proc/self/status", "r", encoding="utf-8") as handle:
                for line in handle:
                    if line.startswith("VmRSS:"):
                        parts = line.split()
                        if len(parts) >= 2:
                            vmrss_kb = int(parts[1])
                        break
        except Exception:
            vmrss_kb = None
        maxrss_kb = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss or 0)
        details = " ".join(f"{key}={value}" for key, value in extra.items())
        print(f"[chunk-trace] {label} vmrss_kb={vmrss_kb} maxrss_kb={maxrss_kb} {details}", file=sys.stderr, flush=True)

    def _compact_document_metadata(self, artifact: Dict[str, Any], *, fallback_metadata: Dict[str, Any] | None = None) -> Dict[str, Any]:
        fallback_metadata = fallback_metadata or {}
        metrics = artifact.get("metrics")
        if not isinstance(metrics, list):
            metrics = [metrics] if metrics else []
        tables = artifact.get("tables_summary")
        if not isinstance(tables, list):
            tables = [tables] if tables else []
        risks = artifact.get("risks")
        if not isinstance(risks, list):
            risks = [risks] if risks else []
        claims = artifact.get("claims")
        if not isinstance(claims, list):
            claims = [claims] if claims else []

        return {
            "document_name": artifact.get("document_name") or fallback_metadata.get("title") or fallback_metadata.get("filename"),
            "document_type": artifact.get("document_type") or fallback_metadata.get("document_type") or fallback_metadata.get("doc_type"),
            "citation_label": artifact.get("citation_label") or fallback_metadata.get("citation_label"),
            "document_summary": artifact.get("document_summary") or fallback_metadata.get("summary"),
            "metrics": metrics[:12],
            "tables_summary": tables[:8],
            "risks": risks[:10],
            "claims": claims[:10],
            "metric_names": fallback_metadata.get("metric_names") or [
                metric.get("name")
                for metric in metrics
                if isinstance(metric, dict) and metric.get("name")
            ][:12],
            # source_map is ONLY needed for the final answer UI, not for reranking or selection.
            # Removing it saves massive amounts of memory during multi-deal retrieval.
            "source_map": {}, 
        }

    def _document_metadata_for_chunk(self, chunk: DocumentChunk) -> Dict[str, Any]:
        metadata = chunk.metadata or {}
        cache_key = f"{chunk.source_type}:{chunk.source_id}"
        if cache_key in self._document_cache:
            return self._document_cache[cache_key]

        artifact: Dict[str, Any] | None = None
        if chunk.source_type == "document" and chunk.source_id:
            doc = (
                DealDocument.objects
                .only("id", "title", "document_type", "evidence_json", "source_map_json", "table_json", "key_metrics_json")
                .filter(id=chunk.source_id)
                .first()
            )
            if doc:
                stored = doc.evidence_json if isinstance(doc.evidence_json, dict) else {}
                artifact = {
                    "document_name": stored.get("document_name") or doc.title,
                    "document_type": stored.get("document_type") or doc.document_type,
                    "document_summary": stored.get("document_summary") or "",
                    "metrics": stored.get("metrics") or doc.key_metrics_json or [],
                    "tables_summary": stored.get("tables_summary") or doc.table_json or [],
                    "risks": stored.get("risks") or [],
                    "claims": stored.get("claims") or [],
                    "source_map": stored.get("source_map") or doc.source_map_json or {},
                    "citation_label": stored.get("citation_label") or doc.title,
                }
        elif chunk.source_type == "analysis_document" and chunk.source_id:
            doc = (
                FolderAnalysisDocument.objects
                .only("id", "file_name", "document_type", "evidence_json", "source_map_json", "table_json", "key_metrics_json")
                .filter(id=chunk.source_id)
                .first()
            )
            if doc:
                stored = doc.evidence_json if isinstance(doc.evidence_json, dict) else {}
                artifact = {
                    "document_name": stored.get("document_name") or doc.file_name,
                    "document_type": stored.get("document_type") or doc.document_type,
                    "document_summary": stored.get("document_summary") or "",
                    "metrics": stored.get("metrics") or doc.key_metrics_json or [],
                    "tables_summary": stored.get("tables_summary") or doc.table_json or [],
                    "risks": stored.get("risks") or [],
                    "claims": stored.get("claims") or [],
                    "source_map": stored.get("source_map") or doc.source_map_json or {},
                    "citation_label": stored.get("citation_label") or doc.file_name,
                }
        elif chunk.source_type == "extracted_source":
            artifact = {
                "document_name": metadata.get("filename") or metadata.get("title"),
                "document_type": metadata.get("doc_type") or metadata.get("document_type") or "Other",
                "document_summary": metadata.get("summary") or "",
                "metrics": metadata.get("metrics") or [],
                "tables_summary": metadata.get("tables_summary") or [],
                "risks": metadata.get("risks") or [],
                "claims": metadata.get("claims") or [],
                "source_map": metadata.get("source_map") or {},
                "citation_label": metadata.get("citation_label") or metadata.get("filename") or metadata.get("title"),
            }

        compact = self._compact_document_metadata(artifact or {}, fallback_metadata=metadata)
        self._document_cache[cache_key] = compact
        return compact

    def process_intent_and_build_metadata(self, user_message: str, conversation_id: str, history_context: str, audit_log_id: str) -> dict:
        gate = self._decide_query_builder_usage(user_message, history_context)
        answer_prompt = self._stage_settings("answer_generation").get("prompt_template")

        if not gate["used_query_builder"]:
            return {
                "history_context": history_context,
                "context_data": "No fresh deal or document retrieval was run for this turn. Use the recent conversation only.",
                "audit_log_id": audit_log_id,
                "query_plan": json.dumps(
                    {
                        "mode": "conversation_only",
                        "reason": gate["gate_reason"],
                        "user_query": user_message,
                    },
                    default=str,
                ),
                "flow_version": getattr(self.flow_version, "version", None),
                "flow_config_id": str(self.flow_version.id) if getattr(self.flow_version, "id", None) else None,
                "answer_generation_prompt": answer_prompt,
                "used_query_builder": False,
                "gate_mode": "conversation_only",
                "gate_reason": gate["gate_reason"],
                "deals_considered": 0,
                "retrieved_chunk_count": 0,
                "selected_chunk_count": 0,
                "selected_sources": [],
            }

        plan = self._build_query_plan(user_message, conversation_id, active_context=history_context)
        deals = self._get_candidate_deals(plan)
        chunks, chunk_diagnostics = self._search_ranked_chunks(plan, deals)
        if not isinstance(chunk_diagnostics, dict):
            chunk_diagnostics = {}
        serialized_deals = [self._serialize_deal(deal) for deal in deals]
        serialized_chunks = [self._serialize_chunk(item) for item in chunks]
        context_data, context_diagnostics = self._format_context_data(
            plan=plan,
            deals=serialized_deals,
            chunks=serialized_chunks,
            diagnostics=chunk_diagnostics,
        )

        if plan.get("needs_stats") and self._stage_enabled("stats_block"):
            context_data += "\n\n[PIPELINE STATS]\n" + json.dumps({
                "total_deals": Deal.objects.count(),
                "female_led_count": Deal.objects.filter(is_female_led=True).count(),
                "by_industry": list(
                    Deal.objects.values("industry").annotate(count=Count("id")).order_by("-count")[:10]
                ),
            }, default=str)
            max_context_chars = int(self._stage_settings("context_assembly").get("max_context_chars", 60000) or 60000)
            if len(context_data) > max_context_chars:
                context_data = context_data[:max_context_chars] + "\n\n... [TRUNCATED DUE TO CONTEXT LIMITS] ..."

        return {
            "history_context": history_context,
            "context_data": context_data,
            "audit_log_id": audit_log_id,
            "query_plan": plan,
            "flow_version": getattr(self.flow_version, "version", None),
            "flow_config_id": str(self.flow_version.id) if getattr(self.flow_version, "id", None) else None,
            "answer_generation_prompt": answer_prompt,
            "used_query_builder": True,
            "gate_mode": "fresh_retrieval",
            "gate_reason": gate["gate_reason"],
            "planner_requested_deal_limit": plan.get("deal_limit"),
            "planner_requested_chunks_per_deal": plan.get("chunks_per_deal"),
            "effective_deal_limit": plan.get("deal_limit"),
            "effective_chunks_per_deal": chunk_diagnostics.get("effective_chunks_per_deal"),
            "deals_considered": len(deals),
            "retrieved_chunk_count": chunk_diagnostics.get("candidate_chunk_count", 0),
            "selected_chunk_count": len(serialized_chunks),
            "candidate_chunk_count": chunk_diagnostics.get("candidate_chunk_count", 0),
            "deals_selected": len(serialized_deals),
            "chunk_count_by_deal": chunk_diagnostics.get("selected_chunk_count_by_deal", {}),
            "selected_chunk_count_by_deal": chunk_diagnostics.get("selected_chunk_count_by_deal", {}),
            "resolved_named_deal_ids": plan.get("_resolved_named_deal_ids", []),
            "selection_mode": plan.get("selection_mode"),
            "stats_mode": plan.get("stats_mode"),
            "chunks_dropped_by_per_deal_cap": chunk_diagnostics.get("dropped_by_per_deal_cap", 0),
            "chunks_dropped_by_total_cap": chunk_diagnostics.get("dropped_by_total_cap", 0),
            "chunks_dropped_as_duplicates": chunk_diagnostics.get("dropped_as_duplicates", 0),
            "chunks_dropped_by_zero_score": chunk_diagnostics.get("dropped_by_zero_score", 0),
            "max_total_chunks": chunk_diagnostics.get("max_total_chunks"),
            "context_chars_before_trim": context_diagnostics.get("chars_before_trim"),
            "context_chars_after_trim": context_diagnostics.get("chars_after_trim"),
            "truncated_chunk_count": context_diagnostics.get("omitted_chunk_count"),
            "selected_sources": [
                f"{chunk['deal']}|{chunk.get('source_title') or chunk['source_type']}"
                for chunk in serialized_chunks
            ],
        }

    def process_single_deal_build_metadata(
        self,
        user_message: str,
        conversation_id: str,
        history_context: str,
        audit_log_id: str,
        deal_id: str,
    ) -> dict:
        answer_prompt = self._stage_settings("answer_generation").get("prompt_template")
        deal = Deal.objects.get(id=deal_id)
        plan = self._build_query_plan(user_message, conversation_id, active_context=history_context)
        plan["deal_limit"] = 1

        deals = [deal]
        chunks, chunk_diagnostics = self._search_ranked_chunks(plan, deals)
        if not isinstance(chunk_diagnostics, dict):
            chunk_diagnostics = {}

        serialized_deals = [self._serialize_deal(deal)]
        serialized_chunks = [self._serialize_chunk(item) for item in chunks]
        context_data, context_diagnostics = self._format_context_data(
            plan=plan,
            deals=serialized_deals,
            chunks=serialized_chunks,
            diagnostics=chunk_diagnostics,
        )
        saved_context = self._saved_relationship_context_for_deal(deal)
        if saved_context:
            context_data = f"{context_data}\n\n[SAVED RELATED-DEAL CONTEXT]\n{saved_context}"

        return {
            "history_context": history_context,
            "context_data": context_data,
            "deal_context": context_data,
            "audit_log_id": audit_log_id,
            "query_plan": plan,
            "flow_version": getattr(self.flow_version, "version", None),
            "flow_config_id": str(self.flow_version.id) if getattr(self.flow_version, "id", None) else None,
            "answer_generation_prompt": answer_prompt,
            "used_query_builder": True,
            "gate_mode": "deal_scoped_retrieval",
            "gate_reason": "single_deal_scope",
            "planner_requested_deal_limit": 1,
            "planner_requested_chunks_per_deal": plan.get("chunks_per_deal"),
            "effective_deal_limit": 1,
            "effective_chunks_per_deal": chunk_diagnostics.get("effective_chunks_per_deal"),
            "deals_considered": 1,
            "retrieved_chunk_count": chunk_diagnostics.get("candidate_chunk_count", 0),
            "selected_chunk_count": len(serialized_chunks),
            "candidate_chunk_count": chunk_diagnostics.get("candidate_chunk_count", 0),
            "deals_selected": 1,
            "chunk_count_by_deal": chunk_diagnostics.get("selected_chunk_count_by_deal", {}),
            "selected_chunk_count_by_deal": chunk_diagnostics.get("selected_chunk_count_by_deal", {}),
            "resolved_named_deal_ids": [str(deal.id)],
            "selection_mode": plan.get("selection_mode"),
            "stats_mode": plan.get("stats_mode"),
            "chunks_dropped_by_per_deal_cap": chunk_diagnostics.get("dropped_by_per_deal_cap", 0),
            "chunks_dropped_by_total_cap": chunk_diagnostics.get("dropped_by_total_cap", 0),
            "chunks_dropped_as_duplicates": chunk_diagnostics.get("dropped_as_duplicates", 0),
            "chunks_dropped_by_zero_score": chunk_diagnostics.get("dropped_by_zero_score", 0),
            "max_total_chunks": chunk_diagnostics.get("max_total_chunks"),
            "context_chars_before_trim": context_diagnostics.get("chars_before_trim"),
            "context_chars_after_trim": context_diagnostics.get("chars_after_trim"),
            "truncated_chunk_count": context_diagnostics.get("omitted_chunk_count"),
            "selected_sources": [
                f"{chunk['deal']}|{chunk.get('source_title') or chunk['source_type']}"
                for chunk in serialized_chunks
            ],
        }

    def _saved_relationship_context_for_deal(self, deal: Deal) -> str:
        try:
            contexts = deal.relationship_contexts.select_related("related_deal", "created_by").all()[:20]
        except Exception:
            return ""
        payload = []
        for item in contexts:
            selected_ids = [str(value) for value in (item.selected_deal_ids or []) if value]
            if item.related_deal_id and str(item.related_deal_id) not in selected_ids:
                selected_ids.append(str(item.related_deal_id))

            related_deals = []
            if selected_ids:
                deal_by_id = {
                    str(candidate.id): candidate
                    for candidate in Deal.objects.filter(id__in=selected_ids).order_by("title")
                }
                for related_id in selected_ids:
                    candidate = deal_by_id.get(str(related_id))
                    if not candidate:
                        continue
                    current_analysis = candidate.current_analysis if isinstance(candidate.current_analysis, dict) else {}
                    canonical_snapshot = current_analysis.get("canonical_snapshot") if isinstance(current_analysis, dict) else {}
                    deal_model_data = current_analysis.get("deal_model_data") if isinstance(current_analysis, dict) else {}
                    report = (
                        current_analysis.get("analyst_report")
                        or canonical_snapshot.get("analyst_report")
                        or candidate.deal_summary
                        or ""
                    )
                    related_deals.append({
                        "id": str(candidate.id),
                        "title": candidate.title,
                        "industry": candidate.industry,
                        "sector": candidate.sector,
                        "current_phase": candidate.current_phase,
                        "priority": candidate.priority,
                        "funding_ask": candidate.funding_ask,
                        "funding_ask_for": candidate.funding_ask_for,
                        "deal_model_data": deal_model_data if isinstance(deal_model_data, dict) else {},
                        "analysis_excerpt": self._truncate_for_prompt(report, 1800),
                    })

            payload.append({
                "relationship_type": item.relationship_type,
                "analyst_notes": (item.notes or "").strip(),
                "selected_document_ids": item.selected_document_ids or [],
                "selected_chunk_ids": item.selected_chunk_ids or [],
                "related_deals": related_deals,
            })
        return json.dumps(payload, default=str, ensure_ascii=True, indent=2) if payload else ""

    def classify_deal_helper_route(self, user_message: str) -> str:
        lowered = (user_message or "").lower()
        if any(term in lowered for term in ["full rewrite", "rewrite analysis", "rewrite the analysis", "regenerate analysis"]):
            return "analysis_full_rewrite"
        if any(term in lowered for term in ["addendum", "ic note", "financial model", "memo", "risk register", "new analysis", "v2", "v3"]):
            return "analysis_user_directive_addendum"
        if any(term in lowered for term in [
            "competitor", "competitors", "similar deal", "similar deals", "comparable",
            "other deals", "pipeline", "sister", "parent", "subsidiary", "compare",
            "compared", "comparison", " vs ", " versus ", "benchmark", "relative to",
            "how does this compare",
        ]):
            return "related_deals"
        return "current_deal"

    def start_deal_helper_session(self, *, deal_id: str, user_message: str, conversation_id: str, history_context: str = "") -> Dict[str, Any]:
        deal = Deal.objects.get(id=deal_id)
        route = self.classify_deal_helper_route(user_message)
        plan = self._build_query_plan(user_message, conversation_id, active_context=history_context)
        saved_context = self._saved_relationship_context_for_deal(deal)
        documents = list(deal.documents.all().order_by("-created_at"))

        if route == "related_deals":
            # Increase limit for interactive selection so analyst has more choices (e.g. at least 12)
            plan["deal_limit"] = max(int(plan.get("deal_limit") or 12), 12)
            plan["_active_deal_context"] = self._active_deal_context_for_related_selection(deal)
            deals = [candidate for candidate in self._get_candidate_deals(plan) if str(candidate.id) != str(deal.id)]
            serialized_deals = [self._serialize_deal(candidate) for candidate in deals]
            for index, item in enumerate(serialized_deals):
                item["suggested_score"] = item.get("retrieval_score")
                item["rank_reason"] = (
                    getattr(deals[index], "_deal_text_rerank_reason", None)
                    or "Reranked pipeline match"
                )
            self._apply_related_deal_suggestions(serialized_deals, plan)
            return {
                "route": route,
                "query_plan": plan,
                "saved_context": saved_context,
                "candidate_deals": serialized_deals,
                "documents": [],
            }

        return {
            "route": route,
            "query_plan": plan,
            "saved_context": saved_context,
            "candidate_deals": [],
            "documents": self._rank_deal_documents_for_helper(deal, plan, documents),
        }

    def _apply_related_deal_suggestions(self, items: List[Dict[str, Any]], plan: Dict[str, Any]) -> None:
        if not isinstance(plan.get("_active_deal_context"), dict):
            self._apply_dynamic_suggestions(items, score_key="suggested_score")
            return
        suggestion_count = min(3, len(items))
        for index, item in enumerate(items):
            item["suggested"] = index < suggestion_count
            if item["suggested"] and not item.get("rank_reason"):
                item["rank_reason"] = "Top comparable to active deal"

    def _active_deal_context_for_related_selection(self, deal: Deal) -> Dict[str, Any]:
        return {
            "deal_id": str(deal.id),
            "title": deal.title,
            "industry": deal.industry,
            "sector": deal.sector,
            "themes": deal.themes if isinstance(deal.themes, list) else [],
            "summary": self._truncate_for_prompt(deal.deal_summary or "", 1800),
            "funding_ask": deal.funding_ask,
            "funding_ask_for": deal.funding_ask_for,
        }

    def _rank_deal_documents_for_helper(self, deal: Deal, plan: Dict[str, Any], documents: List[DealDocument]) -> List[Dict[str, Any]]:
        chunks, _diagnostics = self._search_ranked_chunks(plan, [deal])
        score_by_document_key: Dict[str, float] = {}
        for item in chunks:
            chunk = item["chunk"]
            score = float(item.get("score") or 0)
            metadata = chunk.metadata or {}
            document_metadata = self._document_metadata_for_chunk(chunk)
            keys = [
                str(chunk.source_id or ""),
                str(metadata.get("document_id") or ""),
                str(metadata.get("title") or ""),
                str(metadata.get("filename") or ""),
                str(document_metadata.get("document_name") or ""),
            ]
            for key in keys:
                normalized = self._normalize_document_match_key(key)
                if normalized:
                    score_by_document_key[normalized] = max(score_by_document_key.get(normalized, 0), score)

        payload = []
        for document in documents:
            keys = [
                str(document.id),
                str(document.onedrive_id or ""),
                str(document.title or ""),
            ]
            score = max(score_by_document_key.get(self._normalize_document_match_key(key), 0) for key in keys)
            payload.append({
                "id": str(document.id),
                "title": document.title,
                "document_type": document.document_type,
                "is_indexed": document.is_indexed,
                "is_ai_analyzed": document.is_ai_analyzed,
                "transcription_status": document.transcription_status,
                "chunking_status": document.chunking_status,
                "file_url": document.file_url,
                "suggested": score > 0,
                "suggested_score": round(score, 3) if score else None,
                "rank_reason": "Reranker found relevant chunks in this document" if score else "Available deal document",
            })

        payload.sort(key=lambda item: (float(item.get("suggested_score") or 0), bool(item.get("is_indexed"))), reverse=True)
        llm_candidates = []
        for item in payload[: min(len(payload), 8)]:
            llm_candidates.append({
                "title": item["title"],
                "summary": self._truncate_for_prompt(
                    next(
                        (
                            (chunk["chunk"].metadata or {}).get("document_summary")
                            for chunk in chunks
                            if self._normalize_document_match_key(str(chunk["chunk"].source_id or "")) == self._normalize_document_match_key(item["id"])
                        ),
                        "",
                    ),
                    900,
                ),
                "context": self._truncate_for_prompt(json.dumps({
                    "document_type": item["document_type"],
                    "is_indexed": item["is_indexed"],
                    "is_ai_analyzed": item["is_ai_analyzed"],
                    "rank_reason": item["rank_reason"],
                }, default=str, ensure_ascii=True), 800),
                "base_score": item["suggested_score"] or 0,
            })

        llm_adjustments = self._text_model_rerank(
            label="document",
            query=self._build_rerank_query(plan),
            active_context=self._build_deal_active_context(deal),
            candidates=llm_candidates,
            candidate_limit=min(len(llm_candidates), 8),
        )
        if llm_adjustments:
            for index, item in enumerate(payload):
                adjustment = llm_adjustments.get(index)
                if not adjustment:
                    continue
                base_score = float(item.get("suggested_score") or 0)
                llm_boost = (float(adjustment.get("relevance_score") or 0) - 50.0) * 1.8
                final_score = round(base_score + llm_boost, 3)
                item["suggested_score"] = final_score if final_score > 0 else None
                item["rank_reason"] = adjustment.get("reason") or item["rank_reason"]
        self._apply_dynamic_suggestions(payload, score_key="suggested_score")
        return payload

    def _normalize_document_match_key(self, value: str) -> str:
        normalized = str(value or "").strip().lower()
        if normalized.endswith(".json"):
            normalized = normalized[:-5]
        return normalized

    def _truncate_for_prompt(self, value: Any, limit: int) -> str:
        text = str(value or "").strip()
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 3)].rstrip() + "..."

    def _suggestion_threshold(self, scores: List[float], *, base_ratio: float = 0.82, min_gap: float = 25.0) -> float:
        if not scores:
            return 0.0
        cleaned = sorted(float(score) for score in scores if score is not None)
        if not cleaned:
            return 0.0
        top = cleaned[-1]
        if top <= 0:
            return 0.0
        median = cleaned[len(cleaned) // 2]
        adaptive_gap = max(min_gap, (top - median) * 0.5)
        return max(top * base_ratio, top - adaptive_gap)

    def _apply_dynamic_suggestions(self, items: List[Dict[str, Any]], *, score_key: str = "suggested_score") -> None:
        scores = [float(item.get(score_key) or 0) for item in items if float(item.get(score_key) or 0) > 0]
        threshold = self._suggestion_threshold(scores)
        for item in items:
            score = float(item.get(score_key) or 0)
            item["suggested"] = score > 0 and score >= threshold
            if item["suggested"] and not item.get("rank_reason"):
                item["rank_reason"] = "High-confidence reranked match"

    def _build_deal_active_context(self, deal: Deal) -> str:
        current_analysis = deal.current_analysis if isinstance(deal.current_analysis, dict) else {}
        canonical_snapshot = current_analysis.get("canonical_snapshot") if isinstance(current_analysis, dict) else {}
        documents = []
        for document in deal.documents.all().order_by("-created_at")[:12]:
            documents.append({
                "title": document.title,
                "type": document.document_type,
                "indexed": document.is_indexed,
                "summary": self._truncate_for_prompt(document.normalized_text or document.extracted_text or "", 700),
            })
        context = {
            "title": deal.title,
            "industry": deal.industry,
            "sector": deal.sector,
            "city": deal.city,
            "funding_ask": deal.funding_ask,
            "funding_ask_for": deal.funding_ask_for,
            "priority": deal.priority,
            "deal_summary": self._truncate_for_prompt(deal.deal_summary or canonical_snapshot.get("analyst_report") or "", 1500),
            "analysis_prompt": self._truncate_for_prompt(deal.analysis_prompt or "", 1000),
            "documents": documents,
        }
        return json.dumps(context, default=str, ensure_ascii=True, indent=2)

    def _text_model_rerank(
        self,
        *,
        label: str,
        query: str,
        active_context: str,
        candidates: List[Dict[str, Any]],
        candidate_limit: int = 8,
    ) -> Dict[int, Dict[str, Any]]:
        if not candidates:
            return {}

        preview = []
        for index, candidate in enumerate(candidates[:candidate_limit]):
            preview.append({
                "index": index,
                "title": self._truncate_for_prompt(candidate.get("title"), 120),
                "summary": self._truncate_for_prompt(candidate.get("summary"), 800),
                "context": self._truncate_for_prompt(candidate.get("context"), 1200),
                "base_score": candidate.get("base_score"),
            })

        prompt = (
            f"You are reranking {label} suggestions for an active deal workflow.\n\n"
            f"Active deal context:\n{self._truncate_for_prompt(active_context, 8000) or 'None'}\n\n"
            f"User query or planner intent:\n{self._truncate_for_prompt(query, 2000) or 'None'}\n\n"
            "Candidate list:\n"
            f"{json.dumps(preview, default=str, ensure_ascii=True, indent=2)}\n\n"
            "Task:\n"
            "Score each candidate from 0 to 100 for relevance to the active deal and the user intent.\n"
            "Use the active deal as the comparison anchor.\n"
            "Prefer candidates that improve the comparison, evidence quality, or deal-specific specificity.\n"
            "Return exactly one JSON object with this shape:\n"
            "{\n"
            '  "results": [\n'
            '    {"index": 0, "relevance_score": 0, "suggested": true, "reason": "short reason", "compare_to_active_deal": "short comparison"}\n'
            "  ]\n"
            "}\n"
            "Do not include any extra text."
        )

        try:
            result = self.ai_service.process_content(
                content=prompt,
                personality_name="default",
                skill_name=None,
                metadata={
                    "response_format": {"type": "json_object"},
                    "temperature": 0.0,
                    "max_tokens": 1800,
                },
                source_type=f"deal_helper_{label}_rerank",
                source_id="hybrid-rerank",
                stream=False,
            )
        except Exception as exc:
            logger.warning("Text-model rerank failed for %s: %s", label, exc)
            return {}

        parsed = result if isinstance(result, dict) else {}
        payload = parsed.get("parsed_json") if isinstance(parsed.get("parsed_json"), dict) else None
        raw_response = parsed.get("response") if isinstance(parsed, dict) else ""
        if payload is None:
            extracted = ResponseParserService.extract_json(raw_response or "")
            if extracted:
                try:
                    payload = json.loads(extracted)
                except Exception:
                    payload = None
        if not isinstance(payload, dict):
            return {}

        results = payload.get("results")
        if not isinstance(results, list):
            return {}

        normalized: Dict[int, Dict[str, Any]] = {}
        for item in results:
            if not isinstance(item, dict):
                continue
            try:
                candidate_index = int(item.get("index"))
                relevance_score = float(item.get("relevance_score"))
            except (TypeError, ValueError):
                continue
            normalized[candidate_index] = {
                "relevance_score": max(0.0, min(100.0, relevance_score)),
                "suggested": bool(item.get("suggested", relevance_score >= 60)),
                "reason": self._truncate_for_prompt(item.get("reason") or item.get("compare_to_active_deal") or "", 220),
                "compare_to_active_deal": self._truncate_for_prompt(item.get("compare_to_active_deal") or "", 220),
            }
        return normalized

    def chunks_for_selected_deals(self, *, plan: Dict[str, Any], deal_ids: List[str], current_deal_id: str | None = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        target_ids = list(deal_ids)
        if current_deal_id and current_deal_id not in target_ids:
            target_ids.append(current_deal_id)

        deals = list(Deal.objects.filter(id__in=target_ids).prefetch_related("phase_logs"))
        chunks, diagnostics = self._search_ranked_chunks(plan, deals)
        serialized = [self._serialize_chunk(item) for item in chunks]
        
        # Mark current deal chunks
        for item in serialized:
            if current_deal_id and str(item.get("deal_id")) == str(current_deal_id):
                item["is_current_deal"] = True

        # In chunks_for_selected_deals, these are pipeline deals, so we just use dynamic suggestions
        self._apply_dynamic_suggestions(serialized, score_key="score")
        return serialized, diagnostics

    def documents_for_selected_deals(self, *, plan: Dict[str, Any], deal_ids: List[str], current_deal_id: str | None = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        self._trace_chunks("documents_for_selected_deals_start", deal_ids=deal_ids)
        target_ids = [str(item) for item in deal_ids if item]
        if current_deal_id and str(current_deal_id) not in target_ids:
            target_ids.append(str(current_deal_id))

        deals = list(Deal.objects.filter(id__in=target_ids).prefetch_related("phase_logs"))
        self._trace_chunks("documents_for_selected_deals_query_ranked_chunks_start")
        chunks, diagnostics = self._search_ranked_chunks(plan, deals)
        self._trace_chunks("documents_for_selected_deals_query_ranked_chunks_done", chunks=len(chunks))
        
        score_by_document_key: Dict[Tuple[str, str], float] = {}
        reason_by_document_key: Dict[Tuple[str, str], str] = {}

        for item in chunks:
            chunk = item["chunk"]
            score = float(item.get("score") or 0)
            metadata = chunk.metadata or {}
            document_metadata = self._document_metadata_for_chunk(chunk)
            deal_id = str(chunk.deal_id or "")
            keys = [
                str(chunk.source_id or ""),
                str(metadata.get("document_id") or ""),
                str(metadata.get("title") or ""),
                str(metadata.get("filename") or ""),
                str(document_metadata.get("document_name") or ""),
            ]
            for key in keys:
                normalized = self._normalize_document_match_key(key)
                if not normalized:
                    continue
                compound_key = (deal_id, normalized)
                if score > score_by_document_key.get(compound_key, 0):
                    score_by_document_key[compound_key] = score
                    reason_by_document_key[compound_key] = item.get("llm_reason") or item.get("rank_reason") or "Reranker found relevant evidence in this document"

        self._trace_chunks("documents_for_selected_deals_fetch_docs_start")
        documents = list(
            DealDocument.objects.filter(deal_id__in=target_ids, is_indexed=True)
            .select_related("deal")
            .only(
                "id", "deal_id", "deal__title", "title", "document_type", 
                "is_indexed", "is_ai_analyzed", "transcription_status", 
                "chunking_status", "file_url", "created_at",
                "onedrive_id"
            )
            .order_by("deal__title", "-created_at")
        )
        self._trace_chunks("documents_for_selected_deals_fetch_docs_done", documents=len(documents))
        
        self._trace_chunks("documents_for_selected_deals_rerank_start")
        document_rerank_scores = self._rerank_selected_deal_documents(plan, documents)
        self._trace_chunks("documents_for_selected_deals_rerank_done")
        
        payload: List[Dict[str, Any]] = []
        for document_index, document in enumerate(documents):
            deal_id = str(document.deal_id)
            keys = [
                str(document.id),
                str(document.onedrive_id or ""),
                str(document.title or ""),
            ]
            scored_keys = [
                (deal_id, self._normalize_document_match_key(key))
                for key in keys
                if self._normalize_document_match_key(key)
            ]
            score = max((score_by_document_key.get(key, 0) for key in scored_keys), default=0)
            direct_document_score = document_rerank_scores.get(document_index, 0.0)
            if direct_document_score:
                score = max(score, direct_document_score)
            
            rank_reason = next(
                (reason_by_document_key.get(key) for key in scored_keys if reason_by_document_key.get(key)),
                "Reranked document match" if direct_document_score else "Available indexed document",
            )
            payload.append({
                "id": str(document.id),
                "deal_id": deal_id,
                "deal": document.deal.title if document.deal else "",
                "title": document.title,
                "document_type": document.document_type,
                "is_indexed": document.is_indexed,
                "is_ai_analyzed": document.is_ai_analyzed,
                "transcription_status": document.transcription_status,
                "chunking_status": document.chunking_status,
                "file_url": document.file_url,
                "is_current_deal": bool(current_deal_id and deal_id == str(current_deal_id)),
                "suggested": score > 0,
                "suggested_score": round(score, 3) if score else None,
                "rank_reason": rank_reason,
            })

        self._trace_chunks("documents_for_selected_deals_guidance_start")
        # Apply guidance adjustment to suggested candidates
        suggested_candidates = [item for item in payload if item["suggested"]]
        if suggested_candidates:
            guidance_ids = [item["id"] for item in suggested_candidates[:HARD_MAX_RERANK_CANDIDATES]]
            guidance_map = {
                doc.id: doc 
                for doc in DealDocument.objects.filter(id__in=guidance_ids)
                .only("id", "title", "document_type", "key_metrics_json", "evidence_json")
            }
            for item in payload:
                doc = guidance_map.get(item["id"])
                if not doc:
                    continue
                score = float(item.get("suggested_score") or 0)
                adjustment = self._document_guidance_adjustment(plan, doc)
                if adjustment:
                    item["suggested_score"] = round(max(0.0, score + adjustment), 3)

        payload.sort(
            key=lambda item: (
                float(item.get("suggested_score") or 0),
                bool(item.get("is_current_deal")),
                item.get("deal") or "",
                item.get("title") or "",
            ),
            reverse=True,
        )
        self._apply_selected_document_suggestions(payload)
        self._trace_chunks("documents_for_selected_deals_done", payload=len(payload))
        return payload, diagnostics

    def _document_guidance_adjustment(self, plan: Dict[str, Any], document: DealDocument) -> float:
        title = str(document.title or "").lower()
        doc_type = str(document.document_type or "").lower()
        
        # Build context preview from metadata instead of raw heavy text
        evidence = document.evidence_json if isinstance(document.evidence_json, dict) else {}
        summary = str(evidence.get("document_summary") or "").lower()
        metrics = json.dumps(document.key_metrics_json or [], default=str).lower()
        
        text_preview = f"{title} {doc_type} {summary[:1500]} {metrics[:1000]}".lower()
        adjustment = 0.0

        legal_markers = ["nda", "non disclosure", "non-disclosure", "confidentiality", "legal"]
        if any(marker in title or marker in doc_type for marker in legal_markers):
            adjustment -= 700.0

        metric_terms = [str(item).lower() for item in plan.get("metric_terms", []) if str(item).strip()]
        metric_intent = bool(metric_terms) or plan.get("evidence_preference") == "metrics"
        if metric_intent:
            metric_doc_markers = [
                "investor deck", "deck", "financial", "model", "xls", "xlsx", "dealinfo",
                "p&l", "profit", "revenue", "ebitda", "margin", "unit economics", "irr",
                "valuation", "pat", "cash flow", "balance sheet",
            ]
            if any(marker in text_preview for marker in metric_doc_markers):
                adjustment += 360.0
            matched_metric_terms = sum(1 for term in metric_terms if term in text_preview)
            if matched_metric_terms:
                adjustment += min(300.0, matched_metric_terms * 75.0)

        return adjustment

    def _apply_selected_document_suggestions(self, items: List[Dict[str, Any]]) -> None:
        for item in items:
            item["suggested"] = False

        by_deal: Dict[str, List[Dict[str, Any]]] = {}
        for item in items:
            by_deal.setdefault(str(item.get("deal_id") or ""), []).append(item)

        for deal_items in by_deal.values():
            ranked = sorted(deal_items, key=lambda item: float(item.get("suggested_score") or 0), reverse=True)
            for item in ranked[:2]:
                if float(item.get("suggested_score") or 0) > 0:
                    item["suggested"] = True
                    if not item.get("rank_reason") or item.get("rank_reason") == "Available indexed document":
                        item["rank_reason"] = "Top document match for this selected deal"

    def _rerank_selected_deal_documents(self, plan: Dict[str, Any], documents: List[DealDocument]) -> Dict[int, float]:
        reranker_model = getattr(self.embed_service, "reranker_model", "")
        if not reranker_model or not documents:
            return {}

        candidate_limit = min(len(documents), HARD_MAX_RERANK_CANDIDATES)
        query = self._build_rerank_query(plan)
        
        rerank_settings = self._stage_settings("chunk_rerank")
        # Use a small hydration batch size to keep memory low
        batch_size = max(1, int(rerank_settings.get("chunk_rerank_batch_size") or 12))
        
        scores: Dict[int, float] = {}
        try:
            for start in range(0, candidate_limit, batch_size):
                end = min(start + batch_size, candidate_limit)
                batch_ids = [doc.id for doc in documents[start:end]]
                
                self._trace_chunks("document_rerank_hydrate_start", batch_start=start, batch_count=len(batch_ids))
                
                # Hydrate only this small batch
                hydrated_batch = list(
                    DealDocument.objects.filter(id__in=batch_ids)
                    .select_related("deal")
                    .only(
                        "id", "deal_id", "deal__title", "deal__industry", "deal__sector",
                        "title", "document_type",
                        "key_metrics_json", "evidence_json", "table_json"
                    )
                )
                
                # Maintain original order for reranking results mapping
                id_to_doc = {doc.id: doc for doc in hydrated_batch}
                target_docs = [id_to_doc.get(doc_id) for doc_id in batch_ids if id_to_doc.get(doc_id)]
                
                self._trace_chunks("document_rerank_batch_call_start", batch_size=len(target_docs))
                
                payload = [self._build_helper_document_text(document) for document in target_docs]
                
                batch_results = self.embed_service.reranker.rerank(
                    model=reranker_model,
                    query=query,
                    documents=payload,
                )
                
                for item in batch_results or []:
                    index = item.get("index")
                    score = item.get("score")
                    if index is None or score is None:
                        continue
                    scores[int(index) + start] = round(float(score) * 1000.0, 3)
                
                self._trace_chunks(
                    "document_rerank_batch_done",
                    batch_start=start,
                    batch_size=len(target_docs),
                    results=len(batch_results or []),
                )
                
                # Explicitly clear batch objects to help GC
                del hydrated_batch
                del target_docs
                del payload
                
        except Exception as exc:
            logger.error("Selected-deal document reranker failed: %s", exc, exc_info=True)
            return {}

        return scores

    def _build_helper_document_text(self, document: DealDocument) -> str:
        # Surgical slice of potentially massive JSON fields to avoid OOM during serialization
        metrics = (document.key_metrics_json or [])[:12]
        evidence = (document.evidence_json or {})
        if isinstance(evidence, dict):
            # Only keep high-level evidence points for reranking context
            evidence = {k: v for k, v in list(evidence.items())[:10] if k != "full_text"}
            
        tables = (document.table_json or [])[:8]
        
        evidence_payload = {
            "key_metrics": metrics,
            "evidence": evidence,
            "tables": tables,
        }
        
        parts = [
            f"Deal: {document.deal.title if document.deal else ''}",
            f"Deal Industry: {document.deal.industry if document.deal else ''}",
            f"Deal Sector: {document.deal.sector if document.deal else ''}",
            f"Document Title: {document.title or ''}",
            f"Document Type: {document.document_type or ''}",
            f"Artifact Metadata: {json.dumps(evidence_payload, default=str, ensure_ascii=True)[:2500]}",
            # User request: Do not include full normalized text in document selection reranking.
            # We rely on the AI-extracted metadata and summary instead.
        ]
        return "\n".join(part for part in parts if part).strip()

    def chunks_for_selected_documents_multi_deal(self, *, plan: Dict[str, Any], document_ids: List[str], current_deal_id: str | None = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        self._trace_chunks("chunks_for_selected_documents_multi_deal_start", document_ids=document_ids)
        selected_documents = list(
            DealDocument.objects.filter(id__in=document_ids, is_indexed=True)
            .select_related("deal")
        )
        self._trace_chunks("chunks_for_selected_documents_multi_deal_fetch_docs_done", documents=len(selected_documents))
        
        deals = list({document.deal_id: document.deal for document in selected_documents if document.deal}.values())
        self._trace_chunks("chunks_for_selected_documents_multi_deal_search_ranked_chunks_start", deals=len(deals))
        chunks, diagnostics = self._search_ranked_chunks(plan, deals)
        self._trace_chunks("chunks_for_selected_documents_multi_deal_search_ranked_chunks_done", chunks=len(chunks))
        
        allowed_keys = set()
        for document in selected_documents:
            deal_id = str(document.deal_id)
            for value in [str(document.id), str(document.onedrive_id or ""), str(document.title or "")]:
                normalized = self._normalize_document_match_key(value)
                if normalized:
                    allowed_keys.add((deal_id, normalized))

        self._trace_chunks("chunks_for_selected_documents_multi_deal_filtering_start", allowed_keys=len(allowed_keys))
        selected = []
        for item in chunks:
            chunk = item["chunk"]
            metadata = chunk.metadata or {}
            document_metadata = self._document_metadata_for_chunk(chunk)
            deal_id = str(chunk.deal_id or "")
            chunk_keys = {
                (deal_id, self._normalize_document_match_key(value))
                for value in [
                    str(chunk.source_id or ""),
                    str(metadata.get("document_id") or ""),
                    str(metadata.get("title") or ""),
                    str(metadata.get("filename") or ""),
                    str(document_metadata.get("document_name") or ""),
                ]
            }
            if allowed_keys.intersection(key for key in chunk_keys if key[1]):
                selected.append(item)
        self._trace_chunks("chunks_for_selected_documents_multi_deal_filtering_done", selected=len(selected))

        if not selected and allowed_keys:
            self._trace_chunks("chunks_for_selected_documents_multi_deal_fallback_start")
            fallback = []
            for chunk in (
                DocumentChunk.objects.filter(deal_id__in=[document.deal_id for document in selected_documents])
                .select_related("deal")
                .order_by("-created_at")
                .iterator(chunk_size=100)
            ):
                metadata = chunk.metadata or {}
                document_metadata = self._document_metadata_for_chunk(chunk)
                deal_id = str(chunk.deal_id or "")
                chunk_keys = {
                    (deal_id, self._normalize_document_match_key(value))
                    for value in [
                        str(chunk.source_id or ""),
                        str(metadata.get("document_id") or ""),
                        str(metadata.get("title") or ""),
                        str(metadata.get("filename") or ""),
                        str(document_metadata.get("document_name") or ""),
                    ]
                }
                if allowed_keys.intersection(key for key in chunk_keys if key[1]):
                    fallback.append({"chunk": chunk, "score": 1.0})
                if len(fallback) >= 120:
                    break
            selected = fallback
            self._trace_chunks("chunks_for_selected_documents_multi_deal_fallback_done", fallback=len(fallback))

        self._trace_chunks("chunks_for_selected_documents_multi_deal_serialization_start")
        serialized = [self._serialize_chunk(item) for item in selected]
        for item in serialized:
            item["is_current_deal"] = bool(current_deal_id and str(item.get("deal_id")) == str(current_deal_id))
        
        self._trace_chunks("chunks_for_selected_documents_multi_deal_apply_suggestions_start")
        self._apply_balanced_selected_document_chunk_suggestions(serialized, selected_documents)
        self._trace_chunks("chunks_for_selected_documents_multi_deal_done", serialized=len(serialized))
        return serialized, diagnostics

    def _apply_balanced_selected_document_chunk_suggestions(self, items: List[Dict[str, Any]], selected_documents: List[DealDocument]) -> None:
        for item in items:
            item["suggested"] = False

        selected_deal_ids: List[str] = []
        for document in selected_documents:
            deal_id = str(document.deal_id or "")
            if deal_id and deal_id not in selected_deal_ids:
                selected_deal_ids.append(deal_id)
        if not selected_deal_ids:
            selected_deal_ids = []
            for item in items:
                deal_id = str(item.get("deal_id") or "")
                if deal_id and deal_id not in selected_deal_ids:
                    selected_deal_ids.append(deal_id)

        deal_count = max(len(selected_deal_ids), 1)
        suggestion_cap = min(HARD_MAX_GLOBAL_CHUNKS, max(12, min(24, deal_count * 6)))
        per_deal_cap = max(2, min(6, (suggestion_cap + deal_count - 1) // deal_count))
        selected_count = 0

        for deal_id in selected_deal_ids:
            deal_items = [item for item in items if str(item.get("deal_id") or "") == deal_id]
            for item in deal_items[:per_deal_cap]:
                item["suggested"] = True
                selected_count += 1
                if not item.get("rank_reason"):
                    item["rank_reason"] = "Top semantic chunk from this selected deal's documents"
                if selected_count >= suggestion_cap:
                    return

        if selected_count >= suggestion_cap:
            return

        for item in items:
            if item.get("suggested"):
                continue
            item["suggested"] = True
            selected_count += 1
            if not item.get("rank_reason"):
                item["rank_reason"] = "Additional high-ranking chunk from selected documents"
            if selected_count >= suggestion_cap:
                break

    def chunks_for_selected_documents(self, *, plan: Dict[str, Any], deal_id: str, document_ids: List[str]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        deal = Deal.objects.get(id=deal_id)
        selected_documents = list(deal.documents.filter(id__in=document_ids))
        chunks, diagnostics = self._search_ranked_chunks(plan, [deal])
        selected = []
        allowed_keys = set()
        for document in selected_documents:
            allowed_keys.update(
                self._normalize_document_match_key(value)
                for value in [str(document.id), str(document.onedrive_id or ""), str(document.title or "")]
            )
        allowed_keys = {key for key in allowed_keys if key}
        for item in chunks:
            chunk = item["chunk"]
            metadata = chunk.metadata or {}
            document_metadata = self._document_metadata_for_chunk(chunk)
            chunk_keys = {
                self._normalize_document_match_key(value)
                for value in [
                    str(chunk.source_id or ""),
                    str(metadata.get("document_id") or ""),
                    str(metadata.get("title") or ""),
                    str(metadata.get("filename") or ""),
                    str(document_metadata.get("document_name") or ""),
                ]
            }
            if allowed_keys.intersection(key for key in chunk_keys if key):
                selected.append(item)
        if not selected and allowed_keys:
            fallback = []
            for chunk in DocumentChunk.objects.filter(deal=deal).select_related("deal").order_by("-created_at").iterator(chunk_size=100):
                metadata = chunk.metadata or {}
                document_metadata = self._document_metadata_for_chunk(chunk)
                chunk_keys = {
                    self._normalize_document_match_key(value)
                    for value in [
                        str(chunk.source_id or ""),
                        str(metadata.get("document_id") or ""),
                        str(metadata.get("title") or ""),
                        str(metadata.get("filename") or ""),
                        str(document_metadata.get("document_name") or ""),
                    ]
                }
                if allowed_keys.intersection(key for key in chunk_keys if key):
                    fallback.append({"chunk": chunk, "score": 1.0})
                if len(fallback) >= 80:
                    break
            selected = [
                item for item in fallback
            ]
        serialized = [self._serialize_chunk(item) for item in selected]
        for index, item in enumerate(serialized):
            item["suggested"] = index < 6
            item["rank_reason"] = item.get("rank_reason") or ("Top reranked evidence chunk" if index < 6 else "Additional selected-document chunk")
        return serialized, diagnostics

    def build_context_from_selection(self, *, plan: Dict[str, Any], deal_ids: List[str], chunks: List[Dict[str, Any]], extra_context: str = "", current_deal_id: str | None = None) -> str:
        target_ids = [str(item) for item in deal_ids if item]
        if current_deal_id and str(current_deal_id) not in target_ids:
            target_ids.append(str(current_deal_id))
        deals_qs = Deal.objects.filter(id__in=target_ids).prefetch_related("phase_logs")
        deals = []
        for deal in deals_qs:
            sd = self._serialize_deal(deal)
            if current_deal_id and str(deal.id) == str(current_deal_id):
                sd["is_primary_deal"] = True
            deals.append(sd)
            
        context_data, _diagnostics = self._format_context_data(plan, deals, chunks)
        if extra_context:
            context_data = f"{context_data}\n\n[SAVED/ANALYST CONTEXT]\n{extra_context}"
        return context_data

    def _decide_query_builder_usage(self, user_message: str, history_context: str) -> Dict[str, Any]:
        if not history_context or "ASSISTANT:" not in history_context:
            return {
                "used_query_builder": True,
                "gate_reason": "No prior assistant context was available for a conversation-only follow-up.",
            }

        original_message = user_message.strip()
        lowered = original_message.lower()
        if not lowered:
            return {
                "used_query_builder": True,
                "gate_reason": "Empty message defaults to the retrieval pipeline.",
            }

        if self._looks_like_retrieval_request(original_message, lowered):
            return {
                "used_query_builder": True,
                "gate_reason": "The follow-up requests fresh retrieval, search, comparison, metrics, or verification.",
            }

        if self._looks_like_conversational_follow_up(lowered):
            return {
                "used_query_builder": False,
                "gate_reason": "The follow-up is a clarification, rewrite, or formatting request about the existing conversation.",
            }

        return {
            "used_query_builder": True,
            "gate_reason": "The follow-up did not clearly match a safe conversation-only pattern.",
        }

    def _looks_like_retrieval_request(self, original_message: str, lowered_message: str) -> bool:
        if any(term in lowered_message for term in FORCE_RETRIEVAL_TERMS):
            return True

        quoted_terms = re.findall(r'"([^"]+)"', original_message)
        if quoted_terms:
            return True

        title_case_candidates = re.findall(r"\b[A-Z][a-zA-Z0-9&.-]+(?:\s+[A-Z][a-zA-Z0-9&.-]+){0,3}\b", original_message)
        if title_case_candidates:
            return True

        return False

    def _looks_like_conversational_follow_up(self, lowered_message: str) -> bool:
        if any(re.search(pattern, lowered_message, flags=re.IGNORECASE) for pattern in FOLLOW_UP_SKIP_PATTERNS):
            return True

        conversational_terms = [
            "that", "this", "it", "the answer", "your answer", "last answer",
            "more detail", "more details", "bullet", "bullets", "table",
            "email", "memo", "note", "notes", "action items",
        ]
        return any(term in lowered_message for term in conversational_terms)

    def _build_query_plan(self, user_message: str, conversation_id: str, active_context: str = "") -> Dict[str, Any]:
        planner_template = self._stage_settings("query_planner").get("prompt_template") or UniversalChatFlowService.build_default_config()["stages"][0]["settings"]["prompt_template"]
        active_context = (active_context or "").strip()
        if len(active_context) > 4000:
            active_context = active_context[-4000:]
        planner_prompt = (
            planner_template
            .replace("{{conversation_id}}", str(conversation_id))
            .replace("{{ conversation_id }}", str(conversation_id))
            .replace("{{active_context}}", active_context or "None")
            .replace("{{ active_context }}", active_context or "None")
            .replace("{{user_message}}", user_message)
            .replace("{{ user_message }}", user_message)
        )
        try:
            result = self._execute_planner_request(planner_prompt)
            if isinstance(result, dict) and not result.get("error"):
                return self._normalize_plan(result, user_message)
        except Exception as e:
            logger.warning("Universal chat planner failed, falling back to heuristics: %s", e)

        return self._heuristic_plan(user_message)

    def _execute_planner_request(self, planner_prompt: str) -> Dict[str, Any]:
        payload = {
            "model": AIRuntimeService.get_planner_model(),
            "prompt": planner_prompt,
            "system": "Return exactly one valid JSON object. Do not include markdown, comments, prose, or thinking.",
            "response_format": {"type": "json_object"},
            "options": {
                "max_tokens": 1800,
                "temperature": 0.0,
            },
        }
        try:
            data = self.ai_service.provider.execute_standard(payload, timeout=180)
        except Exception:
            # Some OpenAI-compatible servers reject response_format. Retry with
            # prompt-only JSON enforcement before falling back to heuristics.
            payload.pop("response_format", None)
            data = self.ai_service.provider.execute_standard(payload, timeout=180)
        raw_response = (data.get("response") or "").strip()
        parsed = self._parse_planner_response(raw_response)
        if not isinstance(parsed, dict):
            raise ValueError("Planner did not return a JSON object")
        return parsed

    def _parse_planner_response(self, raw_response: str) -> Dict[str, Any]:
        if not raw_response:
            raise ValueError("Planner returned an empty response")
        try:
            return json.loads(raw_response)
        except json.JSONDecodeError:
            extracted = ResponseParserService.extract_json(raw_response)
            return json.loads(extracted)

    def _normalize_plan(self, plan: Dict[str, Any], user_message: str) -> Dict[str, Any]:
        if self._plan_contains_placeholder_values(plan):
            return self._heuristic_plan(user_message)

        fallback_query_type = str(self._stage_settings("query_planner").get("fallback_query_type") or "pipeline_search").lower()
        query_type = str(plan.get("query_type") or fallback_query_type).lower()
        if query_type not in QUERY_TYPES:
            query_type = fallback_query_type if fallback_query_type in QUERY_TYPES else "pipeline_search"

        planner_settings = self._stage_settings("query_planner")
        max_deal_limit = self._cap(
            max(int(planner_settings.get("max_deal_limit") or planner_settings.get("default_deal_limit") or 8), 3),
            HARD_MAX_DEAL_LIMIT,
        )
        max_chunks_per_deal = self._cap(
            max(int(planner_settings.get("max_chunks_per_deal") or planner_settings.get("default_chunks_per_deal") or 4), 1),
            HARD_MAX_CHUNKS_PER_DEAL,
        )
        hard_filters = plan.get("hard_filters")
        if not isinstance(hard_filters, dict):
            hard_filters = plan.get("deal_filters") or {}

        semantic_queries = self._normalize_string_list(plan.get("semantic_queries"))
        if not semantic_queries:
            semantic_queries = self._normalize_string_list(plan.get("rag_queries"))
        if not self.disable_hard_caps:
            semantic_queries = semantic_queries[:HARD_MAX_SEMANTIC_QUERIES]

        result_shape = str(plan.get("result_shape") or "").lower().strip()
        if result_shape not in RESULT_SHAPES:
            result_shape = {
                "exact_lookup": "single_deal",
                "comparison": "named_set",
                "stats": "cross_pipeline",
            }.get(query_type, "shortlist")

        selection_mode = str(plan.get("selection_mode") or "").lower().strip()
        if selection_mode not in SELECTION_MODES:
            selection_mode = {
                "single_deal": "depth_first",
                "named_set": "depth_first",
                "shortlist": "balanced",
                "cross_pipeline": "breadth_first",
            }.get(result_shape, "balanced")

        evidence_preference = str(plan.get("evidence_preference") or "").lower().strip()
        if evidence_preference not in EVIDENCE_PREFERENCES:
            evidence_preference = "mixed"

        stats_mode = str(plan.get("stats_mode") or "").lower().strip()
        if stats_mode not in STATS_MODES:
            stats_mode = "count" if query_type == "stats" else "none"

        named_entities = self._normalize_named_entities(plan.get("named_entities"))
        if not named_entities:
            named_entities = self._normalize_string_entities(self._normalize_string_list(plan.get("exact_terms")))
        unique_named_deal_terms = []
        for entity in named_entities:
            if entity.get("type") != "deal":
                continue
            text = str(entity.get("text") or "").strip().lower()
            if text and text not in unique_named_deal_terms:
                unique_named_deal_terms.append(text)
        user_message_lower = str(user_message or "").strip().lower()

        default_global_chunk_limit = int(plan.get("global_chunk_limit") or 0)
        if default_global_chunk_limit <= 0:
            default_global_chunk_limit = max(
                0,
                min(
                    int(planner_settings.get("default_chunks_per_deal") or 8) * max(int(plan.get("deal_limit") or planner_settings.get("default_deal_limit") or 8), 1),
                    int(self._stage_settings("context_assembly").get("max_total_chunks") or 80),
                ),
            )

        normalized = {
            "query_type": query_type,
            "hard_filters": {},
            "named_entities": named_entities,
            "exact_terms": self._normalize_string_list(plan.get("exact_terms")),
            "semantic_queries": semantic_queries,
            "soft_constraints": self._normalize_string_list(plan.get("soft_constraints")),
            "metric_terms": self._normalize_string_list(plan.get("metric_terms")),
            "evidence_preference": evidence_preference,
            "result_shape": result_shape,
            "selection_mode": selection_mode,
            "needs_stats": bool(plan.get("needs_stats") or query_type == "stats" or stats_mode != "none"),
            "stats_mode": stats_mode,
            "deal_limit": min(max(int(plan.get("deal_limit") or planner_settings.get("default_deal_limit") or 8), 3), max_deal_limit),
            "chunks_per_deal": min(max(int(plan.get("chunks_per_deal") or planner_settings.get("default_chunks_per_deal") or 2), 1), max_chunks_per_deal),
            "global_chunk_limit": max(
                0,
                self._min_with_optional_hard_cap(
                    [
                        int(plan.get("global_chunk_limit") or default_global_chunk_limit),
                        int(self._stage_settings("context_assembly").get("max_total_chunks") or 80),
                    ],
                    HARD_MAX_GLOBAL_CHUNKS,
                ),
            ),
            "user_query": user_message,
        }

        for field in ["title", "industry", "sector", "city", "priority", "current_phase", "is_female_led", "management_meeting"]:
            value = hard_filters.get(field)
            if value not in [None, "", "null", "None"]:
                normalized["hard_filters"][field] = value

        if not normalized["semantic_queries"]:
            normalized["semantic_queries"] = [user_message]
        if not normalized["metric_terms"]:
            normalized["metric_terms"] = [
                term for term in self._tokenize_keywords(user_message)
                if term.lower() in METRIC_TOKENS
            ]
        if normalized["result_shape"] == "single_deal":
            normalized["deal_limit"] = 1
            normalized["selection_mode"] = "depth_first"
            normalized["chunks_per_deal"] = max(normalized["chunks_per_deal"], 8)
            normalized["global_chunk_limit"] = max(
                normalized["global_chunk_limit"],
                min(
                    int(self._stage_settings("context_assembly").get("max_total_chunks") or 80),
                    max(normalized["chunks_per_deal"] * 3, 24),
                ),
            )
        elif normalized["result_shape"] == "named_set" and normalized["named_entities"]:
            normalized["deal_limit"] = min(max(len(normalized["named_entities"]), 1), max_deal_limit)
            normalized["selection_mode"] = "depth_first"
        elif (
            len(unique_named_deal_terms) == 1
            and normalized["stats_mode"] == "none"
            and unique_named_deal_terms[0] in user_message_lower
        ):
            normalized["result_shape"] = "single_deal"
            normalized["deal_limit"] = 1
            normalized["selection_mode"] = "depth_first"
            normalized["chunks_per_deal"] = max(normalized["chunks_per_deal"], 8)
            normalized["global_chunk_limit"] = max(
                normalized["global_chunk_limit"],
                min(
                    int(self._stage_settings("context_assembly").get("max_total_chunks") or 80),
                    max(normalized["chunks_per_deal"] * 3, 24),
                ),
            )
        if normalized["stats_mode"] != "none":
            normalized["needs_stats"] = True
        normalized["global_chunk_limit"] = self._cap(int(normalized["global_chunk_limit"] or 0), HARD_MAX_GLOBAL_CHUNKS)
        normalized["deal_limit"] = self._cap(int(normalized["deal_limit"] or 0), HARD_MAX_DEAL_LIMIT)
        normalized["chunks_per_deal"] = self._cap(int(normalized["chunks_per_deal"] or 0), HARD_MAX_CHUNKS_PER_DEAL)
        return normalized

    def _heuristic_plan(self, user_message: str) -> Dict[str, Any]:
        planner_settings = self._stage_settings("query_planner")

        plan = {
            "query_type": str(planner_settings.get("fallback_query_type") or "pipeline_search").lower(),
            "hard_filters": {},
            "named_entities": [],
            "exact_terms": [],
            "semantic_queries": [user_message],
            "soft_constraints": [],
            "metric_terms": [],
            "evidence_preference": "mixed",
            "result_shape": "shortlist",
            "selection_mode": "balanced",
            "needs_stats": False,
            "stats_mode": "none",
            "deal_limit": self._cap(int(planner_settings.get("default_deal_limit") or 8), HARD_MAX_DEAL_LIMIT),
            "chunks_per_deal": self._cap(int(planner_settings.get("default_chunks_per_deal") or 2), HARD_MAX_CHUNKS_PER_DEAL),
            "global_chunk_limit": min(
                int(self._stage_settings("context_assembly").get("max_total_chunks") or 80),
                int(planner_settings.get("default_deal_limit") or 8) * int(planner_settings.get("default_chunks_per_deal") or 2),
            ),
            "user_query": user_message,
        }
        plan["global_chunk_limit"] = self._cap(plan["global_chunk_limit"], HARD_MAX_GLOBAL_CHUNKS)
        return plan

    def _plan_contains_placeholder_values(self, plan: Dict[str, Any]) -> bool:
        if not isinstance(plan, dict):
            return True

        def contains_placeholder(value: Any) -> bool:
            if value is None:
                return False
            if isinstance(value, str):
                normalized = value.strip().lower()
                return normalized in PLACEHOLDER_PLAN_VALUES
            if isinstance(value, list):
                return any(contains_placeholder(item) for item in value)
            if isinstance(value, dict):
                return any(contains_placeholder(item) for item in value.values())
            return False

        return contains_placeholder(plan)

    def _normalize_named_entities(self, values: Any) -> List[Dict[str, Any]]:
        if not isinstance(values, list):
            return []
        normalized: List[Dict[str, Any]] = []
        for item in values:
            if isinstance(item, str):
                text = item.strip()
                if text:
                    normalized.append({"type": "deal", "text": text, "confidence": 0.8})
                continue
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            entity_type = str(item.get("type") or "deal").strip().lower()
            if entity_type not in ENTITY_TYPES:
                entity_type = "deal"
            try:
                confidence = float(item.get("confidence", 0.8))
            except (TypeError, ValueError):
                confidence = 0.8
            normalized.append({
                "type": entity_type,
                "text": text,
                "confidence": max(0.0, min(confidence, 1.0)),
            })
        return normalized[:10]

    def _normalize_string_entities(self, values: List[str]) -> List[Dict[str, Any]]:
        normalized: List[Dict[str, Any]] = []
        for value in values:
            text = str(value or "").strip()
            if not text:
                continue
            normalized.append({"type": "deal", "text": text, "confidence": 0.8})
        return normalized[:10]

    def _infer_named_deal_from_query_if_needed(self, queryset, plan: Dict[str, Any]) -> None:
        if self._extract_named_deal_terms(plan):
            return
        if (plan.get("stats_mode") or "none") != "none":
            return
        if plan.get("result_shape") in {"cross_pipeline", "shortlist"} and plan.get("query_type") == "stats":
            return

        user_query = str(plan.get("user_query") or "").strip()
        if not self._looks_like_single_deal_question(user_query):
            return

        match = self._best_deal_title_mention(queryset, user_query)
        if not match:
            return

        title, confidence = match
        plan["named_entities"] = [{"type": "deal", "text": title, "confidence": confidence}]
        plan["exact_terms"] = [title]
        plan["result_shape"] = "single_deal"
        plan["selection_mode"] = "depth_first"
        plan["deal_limit"] = 1
        plan["chunks_per_deal"] = max(int(plan.get("chunks_per_deal") or 0), 8)
        plan["global_chunk_limit"] = max(
            int(plan.get("global_chunk_limit") or 0),
            min(int(self._stage_settings("context_assembly").get("max_total_chunks") or 80), 24),
        )

    def _looks_like_single_deal_question(self, user_query: str) -> bool:
        lowered = user_query.lower()
        single_deal_markers = [
            "tell me about",
            "what do you think about",
            "deep dive",
            "go through",
            "documents",
            "document",
            "financial reports",
            "annual reports",
            "metrics",
            "risks in",
            "risks for",
            "about",
        ]
        broad_markers = [
            "best",
            "top",
            "shortlist",
            "how many",
            "count",
            "compare",
            "between",
            "all deals",
            "in the system",
        ]
        return any(marker in lowered for marker in single_deal_markers) and not any(
            marker in lowered for marker in broad_markers
        )

    def _best_deal_title_mention(self, queryset, user_query: str) -> tuple[str, float] | None:
        query_tokens = {
            token
            for token in self._token_set(user_query)
            if token not in {
                "tell", "about", "what", "think", "deep", "dive", "into", "from",
                "documents", "document", "financial", "reports", "report", "metrics",
                "deal", "company", "have", "system", "please", "can", "you",
            }
        }
        if not query_tokens:
            return None

        best_title = None
        best_score = 0.0
        second_score = 0.0

        for title in queryset.exclude(title__isnull=True).exclude(title="").values_list("title", flat=True).distinct()[:1000]:
            title = str(title or "").strip()
            title_tokens = self._token_set(title)
            if not title_tokens:
                continue

            exact_overlap = query_tokens & title_tokens
            fuzzy_hits = 0
            for query_token in query_tokens:
                if query_token in title_tokens:
                    continue
                if any(SequenceMatcher(None, query_token, title_token).ratio() >= 0.86 for title_token in title_tokens):
                    fuzzy_hits += 1

            overlap_count = len(exact_overlap) + fuzzy_hits
            if overlap_count <= 0:
                continue

            containment = overlap_count / max(len(query_tokens), 1)
            title_coverage = overlap_count / max(min(len(title_tokens), 6), 1)
            text_similarity = SequenceMatcher(None, user_query.lower(), title.lower()).ratio()
            score = (containment * 0.55) + (title_coverage * 0.3) + (text_similarity * 0.15)

            if score > best_score:
                second_score = best_score
                best_score = score
                best_title = title
            elif score > second_score:
                second_score = score

        if best_title and best_score >= 0.42 and best_score - second_score >= 0.08:
            return best_title, min(0.95, max(0.75, best_score))
        return None

    def _get_candidate_deals(self, plan: Dict[str, Any]) -> List[Deal]:
        return self._compute_candidate_deals(plan)

    def _compute_candidate_deals(self, plan: Dict[str, Any]) -> List[Deal]:
        filter_settings = self._stage_settings("deal_filtering")
        rerank_settings = self._stage_settings("chunk_rerank")
        result_limit = max(int(plan.get("deal_limit") or filter_settings.get("result_limit") or 8), 1)
        base_queryset = Deal.objects.all().select_related("retrieval_profile").prefetch_related("phase_logs")
        queryset = base_queryset
        filters = self._align_hard_filters_to_known_values(base_queryset, plan.get("hard_filters", {}))
        plan["hard_filters"] = filters
        self._infer_named_deal_from_query_if_needed(base_queryset, plan)

        if "is_female_led" in filters:
            queryset = queryset.filter(is_female_led=filters["is_female_led"])
        if "management_meeting" in filters:
            queryset = queryset.filter(management_meeting=filters["management_meeting"])
        for field in ["title", "industry", "sector", "city", "priority", "current_phase"]:
            value = filters.get(field)
            if value:
                queryset = queryset.filter(**{f"{field}__icontains": str(value)})

        semantic_limit = max(
            int(filter_settings.get("result_limit") or plan.get("deal_limit") or 20),
            int(plan.get("deal_limit") or 8) * 3,
        )
        resolved_named_deals = self._resolve_named_entity_deals(queryset, plan, limit=max(result_limit, 8))
        if resolved_named_deals:
            plan["_resolved_named_deal_ids"] = [str(deal.id) for deal in resolved_named_deals]
            self._promote_plan_from_resolved_named_deals(plan, resolved_named_deals)
            result_limit = max(int(plan.get("deal_limit") or result_limit), 1)

        if (plan.get("stats_mode") or "none") != "none":
            plan["_stats_queryset_count"] = queryset.count()
            if resolved_named_deals:
                return resolved_named_deals[:result_limit]
            if queryset.exists():
                return list(queryset.order_by("-created_at")[:result_limit])

            stats_semantic_matches: List[Deal] = []
            for semantic_query in plan.get("semantic_queries") or [plan["user_query"]]:
                results = self.embed_service.search_deal_profiles(
                    semantic_query,
                    limit=semantic_limit,
                    filters=None,
                )
                stats_semantic_matches = self._merge_deal_pool(stats_semantic_matches, results)
            return list(stats_semantic_matches[:result_limit])

        semantic_matches: List[Deal] = []
        semantic_rank_map: Dict[str, int] = {}
        for semantic_query in plan.get("semantic_queries") or [plan["user_query"]]:
            results = self.embed_service.search_deal_profiles(
                semantic_query,
                limit=semantic_limit,
                filters=filters,
            )
            for idx, deal in enumerate(results):
                deal_id = str(deal.id)
                if deal_id not in semantic_rank_map:
                    semantic_rank_map[deal_id] = idx
                semantic_matches = self._merge_deal_pool(semantic_matches, [deal])
        recent_pool = list(queryset.order_by("-created_at")[: int(filter_settings.get("candidate_pool_limit") or 60)])
        named_entity_pool = self._keyword_candidate_pool(
            queryset,
            plan,
            limit=int(filter_settings.get("candidate_pool_limit") or 60),
        )
        pool = self._merge_deal_pool(resolved_named_deals, semantic_matches, named_entity_pool, recent_pool)
        if not pool:
            return list(semantic_matches[:result_limit]) if semantic_matches else []

        scored: List[Tuple[float, Deal]] = []
        for deal in pool:
            score, components = self._score_deal_candidate(
                deal,
                plan,
                semantic_rank=semantic_rank_map.get(str(deal.id)),
                semantic_limit=semantic_limit,
                rerank_settings=rerank_settings,
            )
            setattr(deal, "_retrieval_score", score)
            setattr(deal, "_retrieval_components", components)
            scored.append((score, deal))

        scored.sort(key=lambda item: (item[0], item[1].created_at), reverse=True)
        scored = self._rerank_deal_candidates(
            scored,
            plan,
            result_limit=result_limit,
            rerank_settings=rerank_settings,
        )
        if isinstance(plan.get("_active_deal_context"), dict):
            scored = self._prioritize_related_comparables(scored, plan)
        scoped_scored = self._apply_result_shape_scope(scored, plan, result_limit=result_limit)
        if scoped_scored:
            scored = scoped_scored

        selected_deals: List[Deal] = []
        for score, deal in scored:
            if score > 0:
                selected_deals.append(deal)
            if len(selected_deals) >= result_limit:
                break

        if not selected_deals:
            if semantic_matches:
                selected_deals = list(semantic_matches[:result_limit])
            else:
                selected_deals = list(pool[:result_limit])

        return list(selected_deals)

    def _prioritize_related_comparables(self, scored: List[Tuple[float, Deal]], plan: Dict[str, Any]) -> List[Tuple[float, Deal]]:
        active_context = plan.get("_active_deal_context") if isinstance(plan.get("_active_deal_context"), dict) else {}
        if not active_context:
            return scored

        def comparable_tier(deal: Deal) -> Tuple[int, float]:
            active_industry = str(active_context.get("industry") or "")
            active_sector = str(active_context.get("sector") or "").strip().lower()
            candidate_industry = str(deal.industry or "").strip().lower()
            candidate_sector = str(deal.sector or "").strip().lower()
            candidate_text = self._deal_combined_text(deal)

            specific_tokens = self._specific_business_tokens(active_industry)
            specific_hits = sum(1 for token in specific_tokens if token in candidate_text)
            if specific_hits:
                return 4, float(specific_hits)
            if active_industry and candidate_industry and active_industry.lower() == candidate_industry:
                return 4, 0.0
            if active_sector and candidate_sector and active_sector == candidate_sector:
                return 2, 0.0
            active_label_tokens = self._token_set(" ".join([active_industry, active_sector]))
            candidate_label_tokens = self._token_set(" ".join([candidate_industry, candidate_sector]))
            if active_label_tokens.intersection(candidate_label_tokens):
                return 1, float(len(active_label_tokens.intersection(candidate_label_tokens)))
            return 0, 0.0

        return sorted(
            scored,
            key=lambda item: (
                comparable_tier(item[1])[0],
                comparable_tier(item[1])[1],
                item[0],
                item[1].created_at,
            ),
            reverse=True,
        )

    def _promote_plan_from_resolved_named_deals(self, plan: Dict[str, Any], resolved_named_deals: List[Deal]) -> None:
        if (plan.get("stats_mode") or "none") != "none":
            return
        resolved_count = len(resolved_named_deals or [])
        if resolved_count <= 0:
            return

        planner_settings = self._stage_settings("query_planner")
        assembly_settings = self._stage_settings("context_assembly")
        max_total_chunks = int(assembly_settings.get("max_total_chunks") or 80)
        planner_default_chunks = max(int(planner_settings.get("default_chunks_per_deal") or 8), 1)

        if resolved_count == 1:
            target_chunks = max(int(plan.get("chunks_per_deal") or 0), planner_default_chunks, 8)
            plan["result_shape"] = "single_deal"
            plan["selection_mode"] = "depth_first"
            plan["deal_limit"] = 1
            plan["chunks_per_deal"] = target_chunks
            plan["global_chunk_limit"] = min(
                max_total_chunks,
                max(int(plan.get("global_chunk_limit") or 0), max(target_chunks * 3, 24)),
            )
            return

        if resolved_count <= 3:
            target_chunks = max(int(plan.get("chunks_per_deal") or 0), max(planner_default_chunks // 2, 6))
            plan["result_shape"] = "named_set"
            plan["selection_mode"] = "depth_first"
            plan["deal_limit"] = resolved_count
            plan["chunks_per_deal"] = target_chunks
            plan["global_chunk_limit"] = min(
                max_total_chunks,
                max(int(plan.get("global_chunk_limit") or 0), target_chunks * resolved_count),
            )

    def _merge_deal_pool(self, *deal_lists: List[Deal]) -> List[Deal]:
        merged: List[Deal] = []
        seen_ids: set[str] = set()
        for deal_list in deal_lists:
            for deal in deal_list or []:
                deal_id = str(deal.id)
                if deal_id in seen_ids:
                    continue
                seen_ids.add(deal_id)
                merged.append(deal)
        return merged

    def _build_deal_rerank_document(self, deal: Deal, plan: Dict[str, Any]) -> str:
        retrieval_profile = getattr(deal, "retrieval_profile", None)
        profile_text = str(getattr(retrieval_profile, "profile_text", "") or "") if retrieval_profile else ""
        summary_block = f"Summary: {(deal.deal_summary or '')[:1200]}"
        metrics_block = "\n".join(
            [
                f"Funding Ask: {deal.funding_ask or ''}",
                f"Funding Ask For: {deal.funding_ask_for or ''}",
            ]
        ).strip()
        risk_block = ""
        if profile_text:
            risk_matches = re.findall(r"(?im)^(?:key risks?|risks?)[:\s].*$", profile_text)
            risk_block = "\n".join(risk_matches[:8]).strip()

        evidence_preference = plan.get("evidence_preference")
        ordered_blocks: List[str] = []
        if plan.get("_active_deal_context"):
            ordered_blocks.extend([summary_block, risk_block, metrics_block])
        elif evidence_preference == "metrics":
            ordered_blocks.extend([metrics_block, summary_block, risk_block])
        elif evidence_preference == "risks":
            ordered_blocks.extend([risk_block, summary_block, metrics_block])
        else:
            ordered_blocks.extend([summary_block, metrics_block, risk_block])

        parts = [
            f"Title: {deal.title or ''}",
            f"Industry: {deal.industry or ''}",
            f"Sector: {deal.sector or ''}",
            f"City: {deal.city or ''}",
            f"Themes: {', '.join(deal.themes if isinstance(deal.themes, list) else [])}",
        ]
        parts.extend(block for block in ordered_blocks if block)
        parts.extend([
            f"Profile: {profile_text[:2200]}",
        ])
        return "\n".join(parts).strip()

    def _rerank_deal_candidates(
        self,
        scored: List[Tuple[float, Deal]],
        plan: Dict[str, Any],
        *,
        result_limit: int,
        rerank_settings: Dict[str, Any],
    ) -> List[Tuple[float, Deal]]:
        reranker_model = getattr(self.embed_service, "reranker_model", "")
        if not reranker_model or not scored:
            return scored

        candidate_limit = max(int(rerank_settings.get("deal_rerank_candidate_limit") or max(result_limit * 2, 24)), result_limit)
        rerank_weight = float(rerank_settings.get("deal_rerank_weight") or 180)
        rerank_input = scored[:candidate_limit]
        documents = [self._build_deal_rerank_document(deal, plan) for _, deal in rerank_input]
        query = self._build_deal_selection_query(plan)
        try:
            results = self.embed_service.reranker.rerank(
                model=reranker_model,
                query=query,
                documents=documents,
            )
        except Exception as exc:
            logger.warning("Deal reranker failed, falling back to heuristic deal ranking: %s", exc)
            return scored

        if not results:
            return scored

        rerank_scores: Dict[int, float] = {}
        for item in results:
            index = item.get("index")
            score = item.get("score")
            if index is None or score is None:
                continue
            rerank_scores[int(index)] = float(score)

        blended: List[Tuple[float, Deal]] = []
        for idx, (base_score, deal) in enumerate(rerank_input):
            rerank_score = rerank_scores.get(idx)
            if rerank_score is not None:
                components = dict(getattr(deal, "_retrieval_components", {}) or {})
                components["deal_rerank"] = round(rerank_score * rerank_weight, 3)
                blended_score = round(base_score + components["deal_rerank"], 3)
                setattr(deal, "_retrieval_score", blended_score)
                setattr(deal, "_retrieval_components", components)
                setattr(deal, "_deal_rerank_score", rerank_score)
                blended.append((blended_score, deal))
            else:
                blended.append((base_score, deal))

        blended.sort(key=lambda item: (item[0], item[1].created_at), reverse=True)
        if len(scored) > candidate_limit:
            blended.extend(scored[candidate_limit:])

        llm_candidates = [
            {
                "title": deal.title,
                "summary": self._truncate_for_prompt(deal.deal_summary or "", 1000),
                "context": self._truncate_for_prompt(self._build_deal_rerank_document(deal, plan), 1800),
                "base_score": round(base_score, 3),
            }
            for base_score, deal in blended[: min(len(blended), 8)]
        ]
        llm_adjustments = self._text_model_rerank(
            label="deal",
            query=self._build_deal_selection_query(plan),
            active_context=json.dumps({
                "query_plan": plan,
                "candidate_scope": "related_deals",
                "active_deal_context": plan.get("_active_deal_context") or {},
                "selection_rule": (
                    "Rank true business competitors/comparables to the active deal first. "
                    "Industry, sector, customer segment, product/service model, revenue model, and customer geography "
                    "outrank generic availability of financial metrics. Financial richness is secondary evidence quality."
                ),
            }, default=str, ensure_ascii=True, indent=2),
            candidates=llm_candidates,
            candidate_limit=min(len(llm_candidates), 8),
        )
        if llm_adjustments:
            rebased: List[Tuple[float, Deal]] = []
            for index, (base_score, deal) in enumerate(blended):
                adjustment = llm_adjustments.get(index)
                if adjustment:
                    # Increased multiplier from 3.0 to 12.0 to allow LLM to significantly impact the final ranking
                    llm_boost = (float(adjustment.get("relevance_score") or 0) - 50.0) * 12.0
                    components = dict(getattr(deal, "_retrieval_components", {}) or {})
                    components["text_model_relevance"] = round(llm_boost, 3)
                    final_score = round(base_score + llm_boost, 3)
                    setattr(deal, "_retrieval_score", final_score)
                    setattr(deal, "_retrieval_components", components)
                    setattr(deal, "_deal_text_rerank_reason", adjustment.get("reason"))
                    rebased.append((final_score, deal))
                else:
                    rebased.append((base_score, deal))
            rebased.sort(key=lambda item: (item[0], item[1].created_at), reverse=True)
            blended = rebased
        return blended

    def _build_deal_selection_query(self, plan: Dict[str, Any]) -> str:
        active_context = plan.get("_active_deal_context") if isinstance(plan.get("_active_deal_context"), dict) else {}
        if not active_context:
            return self._build_rerank_query(plan)

        parts = [
            "Select true pipeline competitors/comparables to the active deal.",
            "Rank by business comparability first: same/adjacent industry, sector, customer, product/service model, revenue model, geography, and deal stage.",
            "Do not rank candidates higher merely because they have financial metrics or richer documents.",
            "The user's requested metric/detail type is for later evidence retrieval after comparable deals are selected.",
            "active_deal: "
            + " | ".join(
                str(value or "").strip()
                for value in [
                    active_context.get("title"),
                    active_context.get("industry"),
                    active_context.get("sector"),
                    active_context.get("summary"),
                ]
                if str(value or "").strip()
            ),
        ]
        return "\n".join(parts)

    def _keyword_candidate_pool(self, queryset, plan: Dict[str, Any], *, limit: int) -> List[Deal]:
        named_terms = self._extract_named_deal_terms(plan)
        q = Q()
        for phrase in named_terms[:8]:
            q |= Q(title__icontains=phrase)
            q |= Q(deal_summary__icontains=phrase)
            q |= Q(retrieval_profile__profile_text__icontains=phrase)
        if not q:
            return []
        return list(queryset.filter(q).distinct().order_by("-created_at")[:limit])

    def _database_exact_match_pool(self, queryset, plan: Dict[str, Any], *, limit: int) -> List[Deal]:
        named_terms = self._extract_named_deal_terms(plan)
        if not named_terms:
            return []
        q = Q()
        for phrase in named_terms[:8]:
            q |= Q(title__icontains=phrase)
            q |= Q(deal_summary__icontains=phrase)
            q |= Q(company_details__icontains=phrase)
            q |= Q(retrieval_profile__profile_text__icontains=phrase)
        if not q:
            return []
        return list(queryset.filter(q).distinct().order_by("-created_at")[:limit])

    def _stats_keyword_candidate_pool(self, queryset, plan: Dict[str, Any], *, limit: int) -> List[Deal]:
        terms = [
            term for term in self._tokenize_keywords(str(plan.get("user_query") or ""))
            if term.lower() not in {
                "how", "many", "count", "number", "total", "system", "deal", "deals",
                "company", "companies", "business", "businesses", "pipeline",
            }
        ]
        q = Q()
        for term in terms[:10]:
            q |= Q(title__icontains=term)
            q |= Q(industry__icontains=term)
            q |= Q(sector__icontains=term)
            q |= Q(deal_summary__icontains=term)
            q |= Q(company_details__icontains=term)
            q |= Q(retrieval_profile__profile_text__icontains=term)
        if not q:
            return []
        return list(queryset.filter(q).distinct().order_by("-created_at")[:limit])

    def _align_hard_filters_to_known_values(self, queryset, filters: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(filters, dict) or not filters:
            return {}

        aligned: Dict[str, Any] = {}
        passthrough_fields = {"is_female_led", "management_meeting"}
        string_fields = ["title", "industry", "sector", "city", "priority", "current_phase"]

        for field, value in filters.items():
            if value in [None, "", "null", "None"]:
                continue
            if field in passthrough_fields:
                aligned[field] = value
                continue
            if field not in string_fields:
                aligned[field] = value
                continue

            raw_value = str(value).strip()
            if not raw_value:
                continue

            direct_qs = queryset.filter(**{f"{field}__icontains": raw_value})
            if direct_qs.exists():
                aligned[field] = raw_value
                continue

            best_match = self._best_matching_field_value(queryset, field, raw_value)
            if best_match:
                aligned[field] = best_match

        return aligned

    def _best_matching_field_value(self, queryset, field: str, raw_value: str) -> str | None:
        candidates = [
            str(item).strip()
            for item in queryset.exclude(**{f"{field}__isnull": True})
            .exclude(**{field: ""})
            .values_list(field, flat=True)
            .distinct()[:500]
            if str(item).strip()
        ]
        if not candidates:
            return None

        query_tokens = self._token_set(raw_value)
        best_candidate = None
        best_score = 0.0

        for candidate in candidates:
            candidate_tokens = self._token_set(candidate)
            token_overlap = 0.0
            if query_tokens and candidate_tokens:
                token_overlap = len(query_tokens.intersection(candidate_tokens)) / len(query_tokens.union(candidate_tokens))
            text_similarity = SequenceMatcher(None, raw_value.lower(), candidate.lower()).ratio()
            contains_bonus = 0.15 if any(token in candidate.lower() for token in query_tokens if len(token) >= 4) else 0.0
            score = (token_overlap * 0.6) + (text_similarity * 0.4) + contains_bonus
            if score > best_score:
                best_score = score
                best_candidate = candidate

        if best_score >= 0.45:
            return best_candidate
        return None

    def _resolve_named_entity_deals(self, queryset, plan: Dict[str, Any], *, limit: int) -> List[Deal]:
        entities = [
            entity for entity in plan.get("named_entities", [])
            if isinstance(entity, dict) and entity.get("type") == "deal" and str(entity.get("text") or "").strip()
        ]
        if not entities:
            return []

        resolved: List[Deal] = []
        used_ids: set[str] = set()
        for entity in entities[:10]:
            term = str(entity.get("text") or "").strip()
            direct_matches = self._database_exact_match_pool(queryset, {"named_entities": [entity], "exact_terms": [term]}, limit=limit)
            candidate = direct_matches[0] if direct_matches else None
            if candidate is None:
                aligned_title = self._best_matching_field_value(queryset, "title", term)
                if aligned_title:
                    candidate = queryset.filter(title__icontains=aligned_title).order_by("-created_at").first()
            if candidate is None:
                continue
            deal_id = str(candidate.id)
            if deal_id in used_ids:
                continue
            used_ids.add(deal_id)
            resolved.append(candidate)
        return resolved[:limit]

    def _token_set(self, text: str) -> set[str]:
        return {
            token.lower()
            for token in re.findall(r"[A-Za-z0-9&.\-']+", text or "")
            if len(token) >= 3
        }

    def _extract_named_deal_terms(self, plan: Dict[str, Any]) -> List[str]:
        terms = []
        for entity in plan.get("named_entities", []):
            if not isinstance(entity, dict) or entity.get("type") != "deal":
                continue
            value = str(entity.get("text") or "").strip()
            if value and value not in terms:
                terms.append(value)
        for value in self._normalize_string_list(plan.get("exact_terms")):
            if value not in terms:
                terms.append(value)
        return terms[:10]

    def _deal_combined_text(self, deal: Deal) -> str:
        retrieval_profile = getattr(deal, "retrieval_profile", None)
        profile_text = getattr(retrieval_profile, "profile_text", "") if retrieval_profile else ""
        fields = [
            str(deal.title or ""),
            str(deal.industry or ""),
            str(deal.sector or ""),
            str(deal.city or ""),
            str(deal.funding_ask or ""),
            str(deal.funding_ask_for or ""),
            str(deal.deal_summary or ""),
            " ".join(str(item) for item in (deal.themes if isinstance(deal.themes, list) else [])),
            str(profile_text or ""),
        ]
        return " ".join(fields).lower()

    def _deal_label_text(self, deal: Deal) -> str:
        fields = [
            str(deal.title or ""),
            str(deal.industry or ""),
            str(deal.sector or ""),
        ]
        return " ".join(fields).lower()

    def _score_deal_candidate(
        self,
        deal: Deal,
        plan: Dict[str, Any],
        *,
        semantic_rank: int | None,
        semantic_limit: int,
        rerank_settings: Dict[str, Any],
    ) -> Tuple[float, Dict[str, float]]:
        title_lower = (deal.title or "").lower()
        combined = self._deal_combined_text(deal)
        named_terms = [term.lower() for term in self._extract_named_deal_terms(plan)]
        exact_terms = [term.lower() for term in plan.get("exact_terms", [])]
        metric_terms = [term.lower() for term in plan.get("metric_terms", [])]
        components: Dict[str, float] = {}
        active_context = plan.get("_active_deal_context") if isinstance(plan.get("_active_deal_context"), dict) else {}

        for phrase in exact_terms + named_terms:
            if not phrase:
                continue
            if phrase == title_lower:
                components["exact_title_match"] = components.get("exact_title_match", 0.0) + 500.0
            elif phrase in title_lower:
                components["title_phrase_match"] = components.get("title_phrase_match", 0.0) + 260.0
            elif phrase in combined:
                components["context_phrase_match"] = components.get("context_phrase_match", 0.0) + 80.0

        if not active_context:
            for metric in metric_terms:
                if metric in combined:
                    components["metric_term"] = components.get("metric_term", 0.0) + float(rerank_settings.get("deal_metric_boost") or 20) + 18.0

        if semantic_rank is not None:
            components["semantic_rank"] = max(0.0, float((semantic_limit - semantic_rank) * 14))
        if plan.get("hard_filters"):
            components["hard_filter_scope"] = 5.0 * len(plan.get("hard_filters", {}))
        if active_context:
            components.update(self._related_deal_comparability_components(deal, active_context))

        score = round(sum(components.values()), 3)
        return score, components

    def _related_deal_comparability_components(self, deal: Deal, active_context: Dict[str, Any]) -> Dict[str, float]:
        components: Dict[str, float] = {}
        active_id = str(active_context.get("deal_id") or "")
        if active_id and str(deal.id) == active_id:
            components["active_deal_exclusion_penalty"] = -1000.0
            return components

        candidate_industry = str(deal.industry or "").strip().lower()
        candidate_sector = str(deal.sector or "").strip().lower()
        active_industry = str(active_context.get("industry") or "").strip().lower()
        active_sector = str(active_context.get("sector") or "").strip().lower()

        # Improved matching for sector/industry with segment awareness
        def get_segments(text: str) -> set[str]:
            return {s.strip().lower() for s in re.split(r"[/,&()]", text) if s.strip()}

        active_sector_segments = get_segments(active_sector)
        candidate_sector_segments = get_segments(candidate_sector)
        if active_sector and candidate_sector:
            if active_sector == candidate_sector:
                components["same_sector"] = 260.0
            elif active_sector_segments & candidate_sector_segments:
                components["sector_segment_match"] = 220.0
            elif active_sector in candidate_sector or candidate_sector in active_sector:
                components["sector_phrase_overlap"] = 180.0

        active_industry_segments = get_segments(active_industry)
        candidate_industry_segments = get_segments(candidate_industry)
        if active_industry and candidate_industry:
            if active_industry == candidate_industry:
                components["same_industry"] = 1000.0
            elif active_industry_segments & candidate_industry_segments:
                components["industry_segment_match"] = 920.0
            else:
                active_tokens = self._token_set(active_industry)
                candidate_tokens = self._token_set(candidate_industry)
                if active_tokens and candidate_tokens:
                    overlap = len(active_tokens.intersection(candidate_tokens)) / len(active_tokens.union(active_tokens))
                    if overlap:
                        components["industry_token_overlap"] = round(overlap * 900.0, 3)

        # Fix: use summary_excerpt which is the correct key from _serialize_deal
        summary_text = str(active_context.get("summary_excerpt") or active_context.get("summary") or "")
        active_label_tokens = self._token_set(
            " ".join(
                str(value or "")
                for value in [active_context.get("industry"), active_context.get("sector"), summary_text]
            )
        )
        candidate_label_tokens = self._token_set(self._deal_label_text(deal))
        if active_label_tokens and candidate_label_tokens:
            overlap = len(active_label_tokens.intersection(candidate_label_tokens)) / max(len(candidate_label_tokens), 1)
            if overlap:
                components["active_label_overlap"] = round(overlap * 120.0, 3)

        active_themes = {
            str(item).strip().lower()
            for item in active_context.get("themes", [])
            if str(item).strip()
        }
        candidate_themes = {
            str(item).strip().lower()
            for item in (deal.themes if isinstance(deal.themes, list) else [])
            if str(item).strip()
        }
        if active_themes and candidate_themes:
            overlap_count = len(active_themes.intersection(candidate_themes))
            if overlap_count:
                components["theme_overlap"] = min(120.0, overlap_count * 40.0)

        has_business_overlap = any(
            key in components
            for key in [
                "same_sector",
                "sector_phrase_overlap",
                "same_industry",
                "industry_token_overlap",
                "active_label_overlap",
                "theme_overlap",
            ]
        )
        if not has_business_overlap:
            components["no_active_business_overlap_penalty"] = -260.0

        return components

    def _specific_business_tokens(self, text: str) -> set[str]:
        broad_terms = {
            "business", "company", "companies", "pipeline", "project", "model", "models",
            "service", "services", "platform", "solutions", "india", "indian",
            "healthcare", "health", "financial", "finance", "consumer", "technology",
            "tech", "bfsi", "retail", "enterprise", "digital", "private", "limited",
            "ltd", "pvt", "sector", "industry",
        }
        return {token for token in self._token_set(text) if token not in broad_terms}

    def _apply_result_shape_scope(
        self,
        scored: List[Tuple[float, Deal]],
        plan: Dict[str, Any],
        *,
        result_limit: int,
    ) -> List[Tuple[float, Deal]]:
        if not scored:
            return scored

        resolved_named_ids = [
            str(deal_id)
            for deal_id in plan.get("_resolved_named_deal_ids", [])
            if str(deal_id).strip()
        ]
        if resolved_named_ids and (plan.get("stats_mode") or "none") == "none":
            scoped = [
                (score, deal)
                for score, deal in scored
                if str(deal.id) in resolved_named_ids
            ]
            if scoped:
                return scoped[:result_limit]

        if plan.get("result_shape") == "single_deal":
            return scored[:1]

        if plan.get("result_shape") == "named_set":
            named_terms = self._extract_named_deal_terms(plan)
            if not named_terms:
                return scored[:result_limit]
            scoped: List[Tuple[float, Deal]] = []
            used_ids: set[str] = set()
            for term in named_terms:
                term_lower = term.lower()
                for score, deal in scored:
                    deal_id = str(deal.id)
                    if deal_id in used_ids:
                        continue
                    if term_lower in self._deal_combined_text(deal):
                        scoped.append((score, deal))
                        used_ids.add(deal_id)
                        break
            return scoped[:result_limit] if scoped else scored[:result_limit]

        return scored

    def _scope_deals_for_chunk_retrieval(self, plan: Dict[str, Any], deals: List[Deal]) -> List[Deal]:
        if not deals:
            return []
        resolved_named_ids = {
            str(deal_id)
            for deal_id in plan.get("_resolved_named_deal_ids", [])
            if str(deal_id).strip()
        }
        if resolved_named_ids and (plan.get("stats_mode") or "none") == "none":
            scoped = [deal for deal in deals if str(deal.id) in resolved_named_ids]
            if scoped:
                return scoped
        if plan.get("result_shape") == "single_deal":
            return deals[:1]
        if plan.get("result_shape") == "named_set":
            named_terms = self._extract_named_deal_terms(plan)
            if not named_terms:
                return deals
            scoped: List[Deal] = []
            used_ids: set[str] = set()
            for term in named_terms:
                term_lower = term.lower()
                for deal in deals:
                    deal_id = str(deal.id)
                    if deal_id in used_ids:
                        continue
                    if term_lower in self._deal_combined_text(deal):
                        scoped.append(deal)
                        used_ids.add(deal_id)
                        break
            return scoped or deals
        if not self._extract_named_deal_terms(plan):
            return deals
        return deals

    def _search_ranked_chunks(self, plan: Dict[str, Any], deals: List[Deal]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        self._trace_chunks("start_search_ranked_chunks", deals=len(deals), selection_mode=plan.get("selection_mode"))
        if (plan.get("stats_mode") or "none") != "none" and int(plan.get("global_chunk_limit") or 0) == 0:
            return [], {
                "candidate_chunk_count": 0,
                "scored_chunk_count": 0,
                "selected_chunk_count": 0,
                "selected_chunk_count_by_deal": {},
                "effective_chunks_per_deal": 0,
                "max_total_chunks": 0,
                "dropped_by_per_deal_cap": 0,
                "dropped_by_total_cap": 0,
                "dropped_as_duplicates": 0,
                "dropped_by_zero_score": 0,
                "chunk_scope_deal_ids": [],
                "chunk_scope_deal_titles": [],
                "selected_chunk_details": [],
                "selected_sources": [],
            }

        retrieval_settings = self._stage_settings("chunk_retrieval")
        scoped_deals = self._scope_deals_for_chunk_retrieval(plan, deals)
        deal_ids = [str(deal.id) for deal in scoped_deals]
        candidate_limit = self._cap(int(retrieval_settings.get("vector_limit") or 60), HARD_MAX_VECTOR_CANDIDATES)
        self._trace_chunks("scoped_deals", scoped_deals=len(scoped_deals), candidate_limit=candidate_limit)
        candidate_chunks: List[DocumentChunk] = []
        semantic_queries = plan.get("semantic_queries") or [plan["user_query"]]
        if not self.disable_hard_caps:
            semantic_queries = semantic_queries[:HARD_MAX_SEMANTIC_QUERIES]
        for semantic_query in semantic_queries:
            self._trace_chunks("semantic_search_start", query=str(semantic_query)[:80])
            query_matches = self.embed_service.search_global_chunks(
                semantic_query,
                limit=candidate_limit,
                deal_ids=deal_ids or None,
            )
            candidate_chunks = self._merge_chunk_pool(candidate_chunks, query_matches)
            self._trace_chunks("semantic_search_done", query_matches=len(query_matches), candidate_chunks=len(candidate_chunks))
        self._trace_chunks("synthesis_augment_start", candidate_chunks=len(candidate_chunks))
        synthesis_document_candidates = self._augment_with_synthesis_document_candidates(candidate_chunks, scoped_deals, plan)
        candidate_chunks = self._merge_chunk_pool(candidate_chunks, synthesis_document_candidates)
        self._trace_chunks(
            "synthesis_augment_done",
            synthesis_document_candidates=len(synthesis_document_candidates),
            candidate_chunks=len(candidate_chunks),
        )
        self._trace_chunks("deal_summary_augment_start", candidate_chunks=len(candidate_chunks))
        candidate_chunks = self._augment_with_deal_summary_candidates(candidate_chunks, scoped_deals)
        self._trace_chunks("deal_summary_augment_done", candidate_chunks=len(candidate_chunks))
        if not candidate_chunks:
            queryset = DocumentChunk.objects.all().select_related("deal")
            if scoped_deals:
                queryset = queryset.filter(deal__in=scoped_deals)
            fallback_limit = self._cap(
                int(retrieval_settings.get("fallback_candidate_limit") or 120),
                HARD_MAX_FALLBACK_CANDIDATES,
            )
            candidate_chunks = list(queryset.order_by("-created_at")[:fallback_limit])
            self._trace_chunks("fallback_chunks_done", candidate_chunks=len(candidate_chunks), fallback_limit=fallback_limit)

        rerank_settings = self._stage_settings("chunk_rerank")
        chunk_rerank_candidate_limit = self._cap(
            int(rerank_settings.get("chunk_rerank_candidate_limit") or candidate_limit),
            HARD_MAX_RERANK_CANDIDATES,
        )
        if len(candidate_chunks) > chunk_rerank_candidate_limit:
            candidate_chunks = candidate_chunks[:chunk_rerank_candidate_limit]

        rerank_query = self._build_rerank_query(plan)
        self._trace_chunks("rerank_start", candidate_chunks=len(candidate_chunks))
        candidate_chunks = self._rerank_candidate_chunks(candidate_chunks, rerank_query, plan)
        self._trace_chunks("rerank_done", candidate_chunks=len(candidate_chunks))
        scored_items: List[Dict[str, Any]] = []
        dropped_by_zero_score = 0

        self._trace_chunks("score_start", candidate_chunks=len(candidate_chunks))
        for chunk in candidate_chunks:
            metadata = chunk.metadata or {}
            score = 0.0

            rerank_score = getattr(chunk, "rerank_score", None)
            if rerank_score is not None:
                score += float(rerank_score) * 100

            distance = getattr(chunk, "distance", None)
            if distance is not None:
                score += max(0.0, 1.0 - float(distance)) * 15

            source_type = (chunk.source_type or "").lower()
            chunk_kind = str(metadata.get("chunk_kind") or "").lower()
            single_deal_depth_first = (
                len(scoped_deals) == 1
                and (plan.get("selection_mode") or "balanced") == "depth_first"
            )
            if source_type == "deal_summary":
                score += 10.0 if single_deal_depth_first else 25.0
            elif source_type == "document":
                score += 28.0 if single_deal_depth_first else 18.0
            elif source_type == "extracted_source":
                score += 24.0 if single_deal_depth_first else 20.0
            elif source_type == "analysis_document":
                score += 24.0 if single_deal_depth_first else 15.0

            score += self._chunk_evidence_prior(chunk_kind, source_type=source_type, evidence_preference=plan.get("evidence_preference"))
            synthesis_rank = getattr(chunk, "_synthesis_document_rank", None)
            if synthesis_rank is not None:
                # The deal synthesis artifact already analyzed this source document.
                # Use that as a prior, while still letting rerank decide exact chunks.
                score += max(4.0, 18.0 - min(float(synthesis_rank), 14.0))

            if score <= 0:
                dropped_by_zero_score += 1
                continue

            scored_items.append({"chunk": chunk, "score": round(score, 3)})

        scored_items.sort(key=lambda item: item["score"], reverse=True)
        llm_candidates = []
        if scored_items:
            self._trace_chunks("score_llm_rerank_context_build_start", scored_items=len(scored_items))
            active_context = json.dumps(
                {
                    "deals": [self._serialize_deal(deal) for deal in scoped_deals[:6]],
                    "query_plan": plan,
                },
                default=str,
                ensure_ascii=True,
                indent=2,
            )
            for item in scored_items[: min(len(scored_items), 8)]:
                chunk = item["chunk"]
                document_metadata = self._document_metadata_for_chunk(chunk)
                llm_candidates.append({
                    "title": document_metadata.get("document_name") or (chunk.metadata or {}).get("title") or (chunk.metadata or {}).get("filename") or chunk.source_type,
                    "summary": self._truncate_for_prompt(document_metadata.get("document_summary") or "", 900),
                    "context": self._truncate_for_prompt(
                        json.dumps({
                            "deal": chunk.deal.title,
                            "source_type": chunk.source_type,
                            "source_id": chunk.source_id,
                            "chunk_kind": (chunk.metadata or {}).get("chunk_kind"),
                            "chunk_text": chunk.content[:1600],
                        }, default=str, ensure_ascii=True),
                        1600,
                    ),
                    "base_score": item["score"],
                })
            self._trace_chunks("score_llm_rerank_call_start", candidates=len(llm_candidates))
            llm_adjustments = self._text_model_rerank(
                label="chunk",
                query=self._build_rerank_query(plan),
                active_context=active_context,
                candidates=llm_candidates,
                candidate_limit=min(len(llm_candidates), 8),
            )
            if llm_adjustments:
                for index, item in enumerate(scored_items):
                    adjustment = llm_adjustments.get(index)
                    if not adjustment:
                        continue
                    llm_boost = (float(adjustment.get("relevance_score") or 0) - 50.0) * 1.2
                    item["score"] = round(item["score"] + llm_boost, 3)
                    item["llm_reason"] = adjustment.get("reason")
                scored_items.sort(key=lambda item: item["score"], reverse=True)
            self._trace_chunks("score_llm_rerank_done")
        self._trace_chunks("score_done", scored_items=len(scored_items), dropped_by_zero_score=dropped_by_zero_score)
        selected: List[Dict[str, Any]] = []
        per_deal_counts: Dict[str, int] = {}
        seen_keys = set()
        max_per_deal, max_total = self._compute_chunk_budgets(plan, scoped_deals)
        dropped_by_per_deal_cap = 0
        dropped_by_total_cap = 0
        dropped_as_duplicates = 0

        for item in scored_items:
            chunk = item["chunk"]
            deal_key = str(chunk.deal_id)
            metadata = chunk.metadata or {}
            chunk_key = self._chunk_selection_key(chunk)
            if chunk_key in seen_keys:
                dropped_as_duplicates += 1
                continue
            if per_deal_counts.get(deal_key, 0) >= max_per_deal:
                dropped_by_per_deal_cap += 1
                continue
            if len(selected) >= max_total:
                dropped_by_total_cap += 1
                continue
            seen_keys.add(chunk_key)
            per_deal_counts[deal_key] = per_deal_counts.get(deal_key, 0) + 1
            selected.append(item)
        self._trace_chunks("select_done", selected=len(selected), max_total=max_total, max_per_deal=max_per_deal)
        diagnostics = {
            "candidate_chunk_count": len(candidate_chunks),
            "scored_chunk_count": len(scored_items),
            "selected_chunk_count": len(selected),
            "selected_chunk_count_by_deal": dict(per_deal_counts),
            "effective_chunks_per_deal": max_per_deal,
            "max_total_chunks": max_total,
            "dropped_by_per_deal_cap": dropped_by_per_deal_cap,
            "dropped_by_total_cap": dropped_by_total_cap,
            "dropped_as_duplicates": dropped_as_duplicates,
            "dropped_by_zero_score": dropped_by_zero_score,
            "synthesis_document_candidate_count": len(synthesis_document_candidates),
            "chunk_scope_deal_ids": [str(deal.id) for deal in scoped_deals],
            "chunk_scope_deal_titles": [str(deal.title or "") for deal in scoped_deals],
            "selected_chunk_details": [
                {
                    "deal_id": str(item["chunk"].deal_id),
                    "deal_title": str(item["chunk"].deal.title or ""),
                    "source_title": (
                        (item["chunk"].metadata or {}).get("title")
                        or (item["chunk"].metadata or {}).get("filename")
                        or item["chunk"].source_type
                    ),
                    "source_type": item["chunk"].source_type,
                    "chunk_index": (item["chunk"].metadata or {}).get("chunk_index"),
                    "score": item["score"],
                }
                for item in selected
            ],
            "selected_sources": [
                f"{item['chunk'].deal.title}|{((item['chunk'].metadata or {}).get('title') or (item['chunk'].metadata or {}).get('filename') or item['chunk'].source_type)}"
                for item in selected
            ],
        }
        return selected, diagnostics

    def _build_chunk_rerank_document(self, chunk: DocumentChunk, plan: Dict[str, Any]) -> str:
        metadata = chunk.metadata or {}
        try:
            document_metadata = self._document_metadata_for_chunk(chunk)
        except Exception:
            document_metadata = {}
        source_title = (
            metadata.get("title")
            or metadata.get("filename")
            or document_metadata.get("document_name")
            or chunk.source_type
        )
        source_type = str(chunk.source_type or "")
        chunk_kind = str(metadata.get("chunk_kind") or "")
        metrics = document_metadata.get("metrics") or []
        tables_summary = document_metadata.get("tables_summary") or []
        risks = document_metadata.get("risks") or []
        document_summary = str(document_metadata.get("document_summary") or "")

        content_block = f"Chunk Text: {str(chunk.content or '')[:2400]}".strip()
        summary_block = f"Document Summary: {document_summary[:1200]}".strip() if document_summary else ""
        metrics_block = ""
        if metrics:
            metrics_block = "Metrics: " + json.dumps(metrics[:6], default=str, ensure_ascii=True)
        tables_block = ""
        if tables_summary:
            tables_block = "Tables: " + json.dumps(tables_summary[:4], default=str, ensure_ascii=True)
        risks_block = ""
        if risks:
            risks_block = "Risks: " + json.dumps(risks[:8], default=str, ensure_ascii=True)

        evidence_preference = str(plan.get("evidence_preference") or "mixed")
        ordered_blocks: List[str] = []
        if evidence_preference == "metrics":
            ordered_blocks.extend([metrics_block, tables_block, summary_block, content_block, risks_block])
        elif evidence_preference == "risks":
            ordered_blocks.extend([risks_block, summary_block, content_block, metrics_block, tables_block])
        elif evidence_preference == "documents":
            ordered_blocks.extend([summary_block, content_block, metrics_block, tables_block, risks_block])
        else:
            ordered_blocks.extend([summary_block, content_block, metrics_block, tables_block, risks_block])

        parts = [
            f"Deal: {chunk.deal.title or ''}",
            f"Source Title: {source_title}",
            f"Source Type: {source_type}",
            f"Chunk Kind: {chunk_kind}",
            f"Document Type: {document_metadata.get('document_type') or metadata.get('doc_type') or ''}",
            f"Citation Label: {document_metadata.get('citation_label') or ''}",
        ]
        parts.extend(block for block in ordered_blocks if block)
        return "\n".join(part for part in parts if part).strip()

    def _rerank_candidate_chunks(
        self,
        candidate_chunks: List[DocumentChunk],
        rerank_query: str,
        plan: Dict[str, Any],
    ) -> List[DocumentChunk]:
        reranker_model = getattr(self.embed_service, "reranker_model", "")
        reranker = getattr(self.embed_service, "reranker", None)
        if not candidate_chunks:
            return candidate_chunks
        if not reranker_model or reranker is None:
            return self.embed_service._rerank_chunks(candidate_chunks, rerank_query, limit=len(candidate_chunks))

        rerank_settings = self._stage_settings("chunk_rerank")
        batch_size = max(1, int(rerank_settings.get("chunk_rerank_batch_size") or 32))
        results: List[Dict[str, Any]] = []
        try:
            for start in range(0, len(candidate_chunks), batch_size):
                batch_chunks = candidate_chunks[start:start + batch_size]
                batch_documents = [
                    self._build_chunk_rerank_document(chunk, plan)
                    for chunk in batch_chunks
                ]
                batch_results = reranker.rerank(
                    model=reranker_model,
                    query=rerank_query,
                    documents=batch_documents,
                )
                for item in batch_results or []:
                    index = item.get("index")
                    if index is None:
                        continue
                    results.append({**item, "index": start + int(index)})
                self._trace_chunks(
                    "rerank_batch_done",
                    batch_start=start,
                    batch_size=len(batch_chunks),
                    results=len(batch_results or []),
                )
        except Exception as exc:
            logger.warning("Chunk reranker failed on enriched documents, falling back to default chunk rerank: %s", exc)
            return self.embed_service._rerank_chunks(candidate_chunks, rerank_query, limit=len(candidate_chunks))

        if not results:
            return self.embed_service._rerank_chunks(candidate_chunks, rerank_query, limit=len(candidate_chunks))

        scored = []
        for item in results:
            index = item.get("index")
            if index is None or index < 0 or index >= len(candidate_chunks):
                continue
            chunk = candidate_chunks[index]
            setattr(chunk, "rerank_score", item.get("score"))
            scored.append((float(item.get("score") or 0.0), chunk))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        deduped: List[DocumentChunk] = []
        seen: set[tuple[str, str, int]] = set()
        for _, chunk in scored:
            metadata = chunk.metadata or {}
            identity = (
                str(chunk.source_id),
                str(metadata.get("chunk_kind")),
                int(metadata.get("chunk_index", 0) or 0),
            )
            if identity in seen:
                continue
            seen.add(identity)
            deduped.append(chunk)
        return deduped or candidate_chunks

    def _merge_chunk_pool(self, *chunk_lists: List[DocumentChunk]) -> List[DocumentChunk]:
        merged: List[DocumentChunk] = []
        seen_ids: set[str] = set()
        for chunk_list in chunk_lists:
            for chunk in chunk_list or []:
                chunk_id = str(getattr(chunk, "id", ""))
                if chunk_id and chunk_id in seen_ids:
                    continue
                if chunk_id:
                    seen_ids.add(chunk_id)
                merged.append(chunk)
        return merged

    def _augment_with_deal_summary_candidates(self, candidate_chunks: List[DocumentChunk], deals: List[Deal]) -> List[DocumentChunk]:
        seen_ids = {str(chunk.id) for chunk in candidate_chunks}
        augmented = list(candidate_chunks)
        if not deals:
            return augmented

        summary_chunks = list(
            DocumentChunk.objects.filter(deal__in=deals, source_type="deal_summary")
            .select_related("deal")
            .order_by("-created_at")[: max(len(deals) * 8, 24)]
        )
        for chunk in summary_chunks:
            chunk_id = str(chunk.id)
            if chunk_id in seen_ids:
                continue
            seen_ids.add(chunk_id)
            augmented.append(chunk)
        return augmented

    def _normalize_document_name(self, value: Any) -> str:
        normalized = str(value or "").strip().lower()
        if not normalized:
            return ""
        normalized = normalized.replace("\\", "/").split("/")[-1]
        if normalized.endswith(".json"):
            normalized = normalized[:-5]
        normalized = re.sub(r"\s+", " ", normalized)
        return normalized

    def _deal_synthesis_document_entries(self, deal: Deal) -> List[Dict[str, Any]]:
        current_analysis = deal.current_analysis if isinstance(deal.current_analysis, dict) else {}
        canonical_snapshot = current_analysis.get("canonical_snapshot") if isinstance(current_analysis.get("canonical_snapshot"), dict) else {}

        raw_entries: List[Any] = []
        for source in (
            canonical_snapshot.get("document_evidence"),
            current_analysis.get("document_evidence"),
            (current_analysis.get("metadata") or {}).get("analysis_input_files") if isinstance(current_analysis.get("metadata"), dict) else None,
        ):
            if isinstance(source, list):
                raw_entries.extend(source)

        entries: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for rank, item in enumerate(raw_entries):
            if isinstance(item, str):
                entry = {"document_name": item}
            elif isinstance(item, dict):
                entry = dict(item)
            else:
                continue

            names = [
                entry.get("document_name"),
                entry.get("source_file"),
                entry.get("filename"),
                entry.get("file_name"),
                entry.get("title"),
                entry.get("citation_label"),
            ]
            normalized_names = [self._normalize_document_name(name) for name in names if name]
            normalized_names = [name for name in normalized_names if name]
            if not normalized_names:
                continue
            identity = normalized_names[0]
            if identity in seen:
                continue
            seen.add(identity)
            entry["_normalized_names"] = normalized_names
            entry["_rank"] = rank
            entries.append(entry)
        return entries

    def _chunk_matches_synthesis_document(self, chunk: DocumentChunk, document_entries: List[Dict[str, Any]]) -> int | None:
        metadata = chunk.metadata or {}
        chunk_names = [
            metadata.get("title"),
            metadata.get("filename"),
            metadata.get("document_name"),
            metadata.get("citation_label"),
            chunk.source_id,
        ]
        normalized_chunk_names = [
            self._normalize_document_name(name)
            for name in chunk_names
            if name
        ]
        normalized_chunk_names = [name for name in normalized_chunk_names if name]
        if not normalized_chunk_names:
            return None

        for entry in document_entries:
            entry_names = entry.get("_normalized_names") or []
            for chunk_name in normalized_chunk_names:
                for entry_name in entry_names:
                    if chunk_name == entry_name or chunk_name.startswith(entry_name) or entry_name.startswith(chunk_name):
                        return int(entry.get("_rank") or 0)
        return None

    def _augment_with_synthesis_document_candidates(
        self,
        candidate_chunks: List[DocumentChunk],
        deals: List[Deal],
        plan: Dict[str, Any],
    ) -> List[DocumentChunk]:
        if not deals:
            return []

        retrieval_settings = self._stage_settings("chunk_retrieval")
        per_deal_limit = self._cap(
            max(int(retrieval_settings.get("synthesis_document_candidate_limit") or 240), 24),
            HARD_MAX_SYNTHESIS_CANDIDATES_PER_DEAL,
        )
        existing_ids = {str(chunk.id) for chunk in candidate_chunks if getattr(chunk, "id", None)}
        augmented: List[DocumentChunk] = []
        evidence_preference = str(plan.get("evidence_preference") or "mixed")
        self._trace_chunks("synthesis_candidates_config", deals=len(deals), per_deal_limit=per_deal_limit)

        for deal in deals:
            document_entries = self._deal_synthesis_document_entries(deal)
            self._trace_chunks(
                "synthesis_deal_entries",
                deal_id=str(deal.id),
                document_entries=len(document_entries),
            )
            if not document_entries:
                continue

            queryset = (
                DocumentChunk.objects.filter(
                    deal=deal,
                    source_type__in=["document", "extracted_source", "analysis_document"],
                )
                .select_related("deal")
                .only("id", "deal_id", "deal__id", "deal__title", "source_type", "source_id", "metadata", "created_at")
                .order_by("-created_at")
            )
            matched: List[DocumentChunk] = []
            scan_limit = max(per_deal_limit * 6, 300)
            self._trace_chunks("synthesis_scan_start", deal_id=str(deal.id), scan_limit=scan_limit)
            for index, chunk in enumerate(queryset[:scan_limit].iterator(chunk_size=50), start=1):
                if index % 100 == 0:
                    self._trace_chunks(
                        "synthesis_scan_progress",
                        deal_id=str(deal.id),
                        scanned=index,
                        matched=len(matched),
                    )
                chunk_id = str(chunk.id)
                if chunk_id in existing_ids:
                    continue
                rank = self._chunk_matches_synthesis_document(chunk, document_entries)
                if rank is None:
                    continue
                setattr(chunk, "_synthesis_document_rank", rank)
                matched.append(chunk)
            self._trace_chunks("synthesis_scan_done", deal_id=str(deal.id), matched=len(matched))

            def sort_key(chunk: DocumentChunk):
                metadata = chunk.metadata or {}
                kind = str(metadata.get("chunk_kind") or "").lower()
                evidence_bonus = -self._chunk_evidence_prior(
                    kind,
                    source_type=str(chunk.source_type or ""),
                    evidence_preference=evidence_preference,
                )
                return (
                    int(getattr(chunk, "_synthesis_document_rank", 9999) or 0),
                    evidence_bonus,
                    int(metadata.get("chunk_index", 0) or 0),
                )

            matched.sort(key=sort_key)
            for chunk in matched[:per_deal_limit]:
                existing_ids.add(str(chunk.id))
                augmented.append(chunk)

        return augmented

    def _chunk_evidence_prior(self, chunk_kind: str, *, source_type: str, evidence_preference: str | None) -> float:
        evidence_preference = evidence_preference or "mixed"
        if evidence_preference == "metrics":
            priorities = {
                "metric": 30.0,
                "table_summary": 24.0,
                "claim": 10.0,
                "normalized_text": 8.0,
                "risk": 4.0,
            }
        elif evidence_preference == "risks":
            priorities = {
                "risk": 30.0,
                "claim": 14.0,
                "normalized_text": 10.0,
                "metric": 6.0,
                "table_summary": 4.0,
            }
        elif evidence_preference == "documents":
            priorities = {
                "normalized_text": 18.0,
                "claim": 14.0,
                "table_summary": 10.0,
                "metric": 8.0,
                "risk": 8.0,
            }
        elif evidence_preference == "timeline":
            priorities = {
                "claim": 18.0,
                "normalized_text": 12.0,
                "metric": 8.0,
                "risk": 6.0,
                "table_summary": 6.0,
            }
        else:
            priorities = {
                "claim": 14.0,
                "normalized_text": 10.0,
                "metric": 8.0,
                "table_summary": 8.0,
                "risk": 8.0,
            }
            if evidence_preference == "summary" and source_type == "deal_summary":
                return 42.0

        bonus = priorities.get(chunk_kind, 0.0)
        if source_type == "deal_summary" and evidence_preference in {"summary", "mixed"}:
            bonus += 12.0
        return bonus

    def _build_rerank_query(self, plan: Dict[str, Any]) -> str:
        parts: List[str] = []
        active_context = plan.get("_active_deal_context") if isinstance(plan.get("_active_deal_context"), dict) else {}
        if active_context:
            parts.append(
                "active_deal: "
                + " | ".join(
                    str(value or "").strip()
                    for value in [
                        active_context.get("title"),
                        active_context.get("industry"),
                        active_context.get("sector"),
                    ]
                    if str(value or "").strip()
                )
            )
            parts.append("related_deal_rule: select same/adjacent industry competitors first, then retrieve requested evidence within them")
        named_entities = [
            str(entity.get("text") or "").strip()
            for entity in plan.get("named_entities", [])
            if isinstance(entity, dict) and str(entity.get("text") or "").strip()
        ]
        if named_entities:
            parts.append("named_entities: " + ", ".join(named_entities[:8]))
        semantic_queries = self._normalize_string_list(plan.get("semantic_queries"))
        if semantic_queries:
            parts.append(" | ".join(semantic_queries[:3]))
        metric_terms = self._normalize_string_list(plan.get("metric_terms"))
        if metric_terms:
            parts.append("metrics: " + ", ".join(metric_terms[:10]))
        evidence_preference = str(plan.get("evidence_preference") or "").strip()
        if evidence_preference:
            parts.append(f"evidence: {evidence_preference}")
        result_shape = str(plan.get("result_shape") or "").strip()
        if result_shape:
            parts.append(f"result_shape: {result_shape}")
        selection_mode = str(plan.get("selection_mode") or "").strip()
        if selection_mode:
            parts.append(f"selection_mode: {selection_mode}")
        stats_mode = str(plan.get("stats_mode") or "").strip()
        if stats_mode and stats_mode != "none":
            parts.append(f"stats_mode: {stats_mode}")
        return " | ".join(part for part in parts if part).strip() or str(plan.get("user_query") or "").strip()

    def _normalized_source_title(self, chunk: DocumentChunk) -> str:
        metadata = chunk.metadata or {}
        title = str(metadata.get("title") or metadata.get("filename") or "").strip().lower()
        if title.endswith(".json"):
            title = title[:-5]
        return title

    def _chunk_selection_key(self, chunk: DocumentChunk):
        metadata = chunk.metadata or {}
        chunk_index = int(metadata.get("chunk_index", 0) or 0)
        chunk_kind = str(metadata.get("chunk_kind") or "").lower()
        normalized_title = self._normalized_source_title(chunk)
        return (
            str(chunk.deal_id),
            (chunk.source_type or "").lower(),
            normalized_title,
            chunk_kind,
            chunk_index,
        )

    def _build_pipeline_overview(self, deals: List[Deal]) -> str:
        total = Deal.objects.count()
        if not deals:
            return f"Total deals in system: {total}. No strongly matching deals were found, so the answer should stay conservative."
        return f"Total deals in system: {total}. Retrieval narrowed the answer context to {len(deals)} candidate deals."

    def _serialize_deal(self, deal: Deal) -> Dict[str, Any]:
        current_analysis = deal.current_analysis or {}
        canonical_snapshot = current_analysis.get("canonical_snapshot") if isinstance(current_analysis, dict) else {}
        recent_timeline = [
            {
                "date": log.changed_at.isoformat(),
                "from_phase": log.from_phase,
                "to_phase": log.to_phase,
                "rationale": log.rationale,
            }
            for log in deal.phase_logs.all().order_by("-changed_at")[:3]
        ]
        
        # Omit the massive analysis_json/current_analysis blob to avoid OOM in large retrieval turns.
        # Everything needed for the context or UI is already extracted into top-level fields.
        compact_analysis = {
            "report": current_analysis.get("report") or "",
            "kind": current_analysis.get("kind"),
            "created_at": current_analysis.get("created_at"),
            "documents_analyzed": current_analysis.get("documents_analyzed", []),
        }

        return {
            "deal_id": str(deal.id),
            "title": deal.title,
            "industry": deal.industry,
            "sector": deal.sector,
            "city": deal.city,
            "priority": deal.priority,
            "current_phase": deal.current_phase,
            "is_female_led": deal.is_female_led,
            "management_meeting": deal.management_meeting,
            "funding_ask": deal.funding_ask,
            "themes": deal.themes if isinstance(deal.themes, list) else [],
            "comments": deal.comments,
            "deal_details": deal.deal_details,
            "reasons_for_passing": deal.reasons_for_passing,
            "has_extracted_documents": getattr(deal, "is_indexed", False) or bool(deal.extracted_text),
            "summary_excerpt": ((canonical_snapshot or {}).get("analyst_report") or deal.deal_summary or "")[: int(self._stage_settings("context_assembly").get("deal_summary_excerpt_chars", 900) or 900)],
            "current_analysis": compact_analysis,
            "recent_timeline": recent_timeline,
            "retrieval_score": getattr(deal, "_retrieval_score", None),
            "retrieval_components": getattr(deal, "_retrieval_components", None),
        }

    def _serialize_chunk(self, item: Dict[str, Any]) -> Dict[str, Any]:
        chunk = item["chunk"]
        excerpt = chunk.content[: int(self._stage_settings("context_assembly").get("chunk_excerpt_chars", 1400) or 1400)]
        metadata = chunk.metadata or {}
        return {
            "chunk_id": str(chunk.id),
            "deal": chunk.deal.title,
            "deal_id": str(chunk.deal_id),
            "source_type": chunk.source_type,
            "source_id": chunk.source_id,
            "source_title": metadata.get("title") or metadata.get("filename"),
            "score": item["score"],
            "suggested": bool(item.get("suggested", False)),
            "suggested_score": item.get("score"),
            "rank_reason": item.get("llm_reason") or item.get("rank_reason"),
            "text": excerpt,
            "metadata": metadata,
            "document_metadata": self._document_metadata_for_chunk(chunk),
        }

    def _format_context_data(self, plan: Dict[str, Any], deals: List[Dict[str, Any]], chunks: List[Dict[str, Any]], diagnostics: Dict[str, Any] | None = None) -> Tuple[str, Dict[str, Any]]:
        diagnostics = diagnostics or {}
        
        # Identify primary deal for explicit labeling
        primary_deal = next((d for d in deals if d.get("is_primary_deal")), None)
        
        sections = [
            "[PIPELINE OVERVIEW]",
            self._build_pipeline_overview_from_payload(deals),
            "",
        ]
        
        if primary_deal:
            sections.extend([
                "[PRIMARY DEAL - ACTIVE CONTEXT]",
                f"THE CURRENT DEAL BEING ANALYZED IS: {primary_deal['title'].upper()}",
                "Use this deal as the anchor for all comparisons and reasoning.",
                "",
            ])

        sections.extend([
            "[QUERY PLAN]",
            json.dumps(plan, default=str, indent=2),
            "",
            "[CANDIDATE DEALS]",
            "* NOTE: Deals marked 'Has Extracted Docs: NO' are legacy records containing ONLY high-level metadata, stats, and comments.",
            "* DO NOT hallucinate or assume the existence of deep-dive financial models or full documents for these deals.",
            "",
        ])

        if deals:
            for deal in deals:
                has_docs_str = "YES" if deal.get("has_extracted_documents") else "NO"
                is_primary_str = " [PRIMARY DEAL]" if deal.get("is_primary_deal") else ""
                sections.append(
                    f"- {deal['title']}{is_primary_str} | Has Extracted Docs: {has_docs_str} | Industry: {deal.get('industry') or 'N/A'} | "
                    f"Sector: {deal.get('sector') or 'N/A'} | Priority: {deal.get('priority') or 'N/A'} | "
                    f"Phase: {deal.get('current_phase') or 'N/A'} | Themes: {', '.join(deal.get('themes') or []) or 'N/A'}"
                )
                if deal.get("summary_excerpt"):
                    sections.append(f"  Summary: {deal['summary_excerpt']}")
                if deal.get("reasons_for_passing"):
                    sections.append(f"  Reasons for Passing: {deal['reasons_for_passing']}")
                if deal.get("comments"):
                    sections.append(f"  Institutional Comments: {deal['comments']}")
                if deal.get("deal_details"):
                    sections.append(f"  Legacy Details: {deal['deal_details']}")
        else:
            sections.append("- No strong candidate deals found.")

        sections.extend(["", "[TOP EVIDENCE CHUNKS]"])
        if chunks:
            for index, chunk in enumerate(chunks, start=1):
                sections.append(
                    f"{index}. [Deal: {chunk['deal']} | Source: {chunk.get('source_title') or chunk['source_type']} | "
                    f"Type: {chunk['source_type']} | Score: {chunk['score']}]"
                )
                if chunk.get("document_metadata"):
                    sections.append(
                        "  Document Metadata: "
                        + json.dumps(chunk["document_metadata"], default=str, ensure_ascii=True)
                    )
                sections.append(chunk["text"])
        else:
            sections.append("- No high-confidence document chunks were selected.")

        chars_before_trim = len("\n".join(sections).strip())
        final_sections = self._trim_sections_to_budget(sections)
        omitted_chunk_count = max(0, len(chunks) - final_sections[1])
        if diagnostics.get("dropped_by_total_cap") or diagnostics.get("dropped_by_per_deal_cap") or omitted_chunk_count:
            final_sections[0].extend([
                "",
                "[RETRIEVAL DIAGNOSTICS]",
                json.dumps(
                    {
                        "selected_chunk_count": len(chunks),
                        "omitted_chunk_count": omitted_chunk_count,
                        "dropped_by_per_deal_cap": diagnostics.get("dropped_by_per_deal_cap", 0),
                        "dropped_by_total_cap": diagnostics.get("dropped_by_total_cap", 0),
                        "selected_chunk_count_by_deal": diagnostics.get("selected_chunk_count_by_deal", {}),
                    },
                    default=str,
                ),
            ])
        text = "\n".join(final_sections[0]).strip()
        return text, {
            "chars_before_trim": chars_before_trim,
            "chars_after_trim": len(text),
            "omitted_chunk_count": omitted_chunk_count,
        }

    def _build_pipeline_overview_from_payload(self, deals: List[Dict[str, Any]]) -> str:
        total = Deal.objects.count()
        if not deals:
            return f"Total deals in system: {total}. No strongly matching deals were found, so the answer should stay conservative."
        return f"Total deals in system: {total}. Retrieval narrowed the answer context to {len(deals)} candidate deals."

    def _normalize_string_list(self, values: Any) -> List[str]:
        if not values:
            return []
        if isinstance(values, str):
            return [values.strip()] if values.strip() else []
        result = []
        for value in values:
            if value is None:
                continue
            text = str(value).strip()
            if text:
                result.append(text)
        return result

    def _tokenize_keywords(self, text: str) -> List[str]:
        tokens = re.findall(r"[A-Za-z0-9%./-]+", text)
        cleaned = []
        seen = set()
        for token in tokens:
            lowered = token.lower()
            if len(lowered) < 3 and lowered.upper() not in {"IC", "CM1", "CM2"}:
                continue
            if lowered in QUERY_STOPWORDS:
                continue
            if lowered in seen:
                continue
            seen.add(lowered)
            cleaned.append(token)
        return cleaned[:20]

    def _stage_settings(self, stage_id: str) -> Dict[str, Any]:
        return UniversalChatFlowService.stage_settings(self.flow_config, stage_id)

    def _stage_enabled(self, stage_id: str) -> bool:
        for stage in self.flow_config.get("stages", []):
            if stage.get("id") == stage_id:
                return bool(stage.get("enabled", True))
        return False

    def _build_analysis_input_summary(
        self,
        *,
        deals: List[Dict[str, Any]],
        chunks: List[Dict[str, Any]],
        context_data: str,
    ) -> Dict[str, Any]:
        selected_deals = [
            {
                "deal_id": str(deal.get("deal_id") or ""),
                "title": str(deal.get("title") or ""),
            }
            for deal in deals
        ]
        selected_chunks = [
            {
                "deal_id": str(chunk.get("deal_id") or ""),
                "deal_title": str(chunk.get("deal") or ""),
                "source_title": str(chunk.get("source_title") or chunk.get("source_type") or ""),
                "source_type": str(chunk.get("source_type") or ""),
                "source_id": str(chunk.get("source_id") or ""),
            }
            for chunk in chunks
        ]
        return {
            "selected_deal_count": len(selected_deals),
            "selected_deals": selected_deals,
            "selected_chunk_count": len(selected_chunks),
            "selected_chunk_sources": [chunk["source_title"] for chunk in selected_chunks],
            "selected_chunk_mappings": selected_chunks,
            "context_chars": len(context_data or ""),
        }

    def _run_analysis_debug(
        self,
        *,
        user_message: str,
        conversation_id: str,
        plan: Dict[str, Any],
        context_data: str,
        history_context: str = "",
        include_prompt: bool = False,
    ) -> Dict[str, Any]:
        answer_prompt = self._stage_settings("answer_generation").get("prompt_template") or ""
        prompt_metadata = {
            "history_context": history_context,
            "context_data": context_data,
            "query_plan": json.dumps(plan, default=str, indent=2),
        }
        rendered_prompt, cleaned_context = PromptBuilderService.build_user_prompt(
            answer_prompt,
            user_message,
            metadata=prompt_metadata,
        )
        analysis_model = AIRuntimeService.get_planner_model()
        personality = AIRuntimeService.get_personality("default")
        system_instructions = PromptBuilderService.build_system_instructions(
            personality,
            skill=None,
            stream=True,
        )
        payload = {
            "model": analysis_model,
            "prompt": rendered_prompt,
            "system": system_instructions,
            "stream": False,
            "options": {
                "max_tokens": 8192,
                "temperature": 0.1,
            },
        }
        result = self.ai_service.provider.execute_standard(payload)
        analysis_answer = result.get("response") or result.get("thinking") or ""
        return {
            "analysis_answer": analysis_answer,
            "analysis_model_used": analysis_model,
            "analysis_context_preview": context_data[:4000],
            "analysis_prompt_preview": rendered_prompt[:8000] if include_prompt else None,
        }

    def simulate_query(
        self,
        user_message: str,
        conversation_id: str = "admin-preview",
        *,
        run_analysis: bool = False,
        include_analysis_prompt: bool = False,
    ) -> Dict[str, Any]:
        plan = self._build_query_plan(user_message, conversation_id)
        deals = self._get_candidate_deals(plan)
        chunks, chunk_diagnostics = self._search_ranked_chunks(plan, deals)
        serialized_deals = [self._serialize_deal(deal) for deal in deals]
        serialized_chunks = [self._serialize_chunk(item) for item in chunks]
        context_preview, context_diagnostics = self._format_context_data(plan, serialized_deals, serialized_chunks, diagnostics=chunk_diagnostics)
        analysis_input_summary = self._build_analysis_input_summary(
            deals=serialized_deals,
            chunks=serialized_chunks,
            context_data=context_preview,
        )
        analysis_payload = {
            "analysis_answer": None,
            "analysis_model_used": None,
            "analysis_input_summary": analysis_input_summary,
            "analysis_context_preview": context_preview[:4000],
            "analysis_prompt_preview": None,
        }
        if run_analysis:
            analysis_payload.update(
                self._run_analysis_debug(
                    user_message=user_message,
                    conversation_id=conversation_id,
                    plan=plan,
                    context_data=context_preview,
                    history_context="",
                    include_prompt=include_analysis_prompt,
                )
            )
        return {
            "flow_version": getattr(self.flow_version, "version", None),
            "query_plan": plan,
            "candidate_deals": serialized_deals,
            "top_chunks": serialized_chunks,
            "context_preview": context_preview[:4000],
            "retrieval_diagnostics": {
                **chunk_diagnostics,
                **context_diagnostics,
                "planner_requested_deal_limit": plan.get("deal_limit"),
                "planner_requested_chunks_per_deal": plan.get("chunks_per_deal"),
                "selection_mode": plan.get("selection_mode"),
                "stats_mode": plan.get("stats_mode"),
                "resolved_named_deal_ids": plan.get("_resolved_named_deal_ids", []),
                "deals_selected": len(serialized_deals),
            },
            "answer_prompt_preview": self._stage_settings("answer_generation").get("prompt_template"),
            **analysis_payload,
        }

    def _compute_chunk_budgets(self, plan: Dict[str, Any], deals: List[Deal]) -> Tuple[int, int]:
        retrieval_settings = self._stage_settings("chunk_retrieval")
        assembly_settings = self._stage_settings("context_assembly")
        requested = max(int(plan.get("chunks_per_deal") or retrieval_settings.get("default_chunks_per_deal") or 2), 1)
        min_per_deal = max(int(assembly_settings.get("min_chunks_per_selected_deal") or 1), 1)
        max_per_deal = max(int(assembly_settings.get("max_chunks_per_selected_deal") or requested), min_per_deal)
        few_deal_threshold = max(int(assembly_settings.get("few_deal_chunk_boost_threshold") or 4), 1)
        few_deal_boost = max(int(assembly_settings.get("few_deal_chunk_boost") or 0), 0)
        single_deal_boost = max(int(assembly_settings.get("single_deal_chunk_boost") or 0), 0)
        deal_count = max(len(deals), 1)

        effective_per_deal = max(requested, min_per_deal)
        selection_mode = plan.get("selection_mode") or "balanced"
        if selection_mode == "depth_first":
            effective_per_deal += max(single_deal_boost, few_deal_boost)
        elif selection_mode == "breadth_first":
            effective_per_deal = max(min_per_deal, min(effective_per_deal, requested))
        elif deal_count == 1:
            effective_per_deal += single_deal_boost
        elif deal_count <= few_deal_threshold:
            effective_per_deal += few_deal_boost
        effective_per_deal = min(effective_per_deal, max_per_deal)
        effective_per_deal = self._cap(effective_per_deal, HARD_MAX_CHUNKS_PER_DEAL)

        soft_total = max(int(assembly_settings.get("soft_max_total_chunks") or effective_per_deal * deal_count), effective_per_deal)
        hard_total = max(int(assembly_settings.get("max_total_chunks") or soft_total), effective_per_deal)
        fallback_total = max(int(assembly_settings.get("fallback_max_total_chunks") or hard_total), effective_per_deal)
        if deal_count == 1:
            effective_total = min(hard_total, max(effective_per_deal, soft_total))
        else:
            effective_total = min(hard_total, max(effective_per_deal * deal_count, fallback_total, soft_total))
        if plan.get("result_shape") == "single_deal":
            effective_per_deal = max(effective_per_deal, 6)
            effective_total = max(effective_total, effective_per_deal)
        global_chunk_limit = int(plan.get("global_chunk_limit") or 0)
        if global_chunk_limit > 0:
            effective_total = min(effective_total, global_chunk_limit)
        effective_total = self._cap(effective_total, HARD_MAX_GLOBAL_CHUNKS)
        return effective_per_deal, effective_total

    def _trim_sections_to_budget(self, sections: List[str]) -> Tuple[List[str], int]:
        assembly_settings = self._stage_settings("context_assembly")
        max_context_chars = self._cap(
            int(assembly_settings.get("max_context_chars", 60000) or 60000),
            HARD_MAX_CONTEXT_CHARS,
        )
        if len("\n".join(sections).strip()) <= max_context_chars:
            return sections, self._count_rendered_chunks(sections)

        trimmed: List[str] = []
        for section in sections:
            candidate = "\n".join(trimmed + [section]).strip()
            if len(candidate) > max_context_chars:
                break
            trimmed.append(section)

        trimmed.append("... [ADDITIONAL CONTEXT OMITTED TO FIT RETRIEVAL BUDGET] ...")
        return trimmed, self._count_rendered_chunks(trimmed)

    def _count_rendered_chunks(self, sections: List[str]) -> int:
        count = 0
        for line in sections:
            if re.match(r"^\d+\.\s+\[Deal:", line):
                count += 1
        return count
