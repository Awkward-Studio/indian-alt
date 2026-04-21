import json
import logging
import re
from typing import Any, Dict, List, Tuple

from django.db import connection
from django.db.models import Count, Q

from deals.models import Deal, DealDocument, FolderAnalysisDocument
from ..models import DocumentChunk
from .ai_processor import AIProcessorService
from .embedding_processor import EmbeddingService
from .flow_config import UniversalChatFlowService
from .runtime import AIRuntimeService
from deals.services.document_artifacts import DocumentArtifactService

logger = logging.getLogger(__name__)


QUERY_TYPES = {
    "exact_lookup",
    "comparison",
    "stats",
    "pipeline_search",
    "timeline",
    "narrative",
}

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
            "metrics": metrics[:10],
            "tables_summary": tables[:6],
            "risks": risks[:8],
            "claims": claims[:8],
            "metric_names": fallback_metadata.get("metric_names") or [
                metric.get("name")
                for metric in metrics
                if isinstance(metric, dict) and metric.get("name")
            ][:10],
            "source_map": artifact.get("source_map") or fallback_metadata.get("source_map") or {},
        }

    def _document_metadata_for_chunk(self, chunk: DocumentChunk) -> Dict[str, Any]:
        metadata = chunk.metadata or {}
        cache_key = f"{chunk.source_type}:{chunk.source_id}"
        if cache_key in self._document_cache:
            return self._document_cache[cache_key]

        artifact: Dict[str, Any] | None = None
        if chunk.source_type == "document" and chunk.source_id:
            doc = DealDocument.objects.filter(id=chunk.source_id).first()
            if doc:
                artifact = DocumentArtifactService.artifact_from_document(doc)
        elif chunk.source_type == "analysis_document" and chunk.source_id:
            doc = FolderAnalysisDocument.objects.filter(id=chunk.source_id).first()
            if doc:
                artifact = DocumentArtifactService.artifact_from_analysis_document(doc)
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

        plan = self._build_query_plan(user_message, conversation_id)
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
        plan = self._build_query_plan(user_message, conversation_id)
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

    def _build_query_plan(self, user_message: str, conversation_id: str) -> Dict[str, Any]:
        planner_template = self._stage_settings("query_planner").get("prompt_template") or UniversalChatFlowService.build_default_config()["stages"][0]["settings"]["prompt_template"]
        planner_prompt = (
            planner_template
            .replace("{{conversation_id}}", str(conversation_id))
            .replace("{{ conversation_id }}", str(conversation_id))
            .replace("{{user_message}}", user_message)
            .replace("{{ user_message }}", user_message)
        )
        try:
            result = self.ai_service.process_content(
                content=planner_prompt,
                stream=False,
                source_type="universal_chat_intent",
                source_id=conversation_id,
                model_override=AIRuntimeService.get_planner_model(),
            )
            if isinstance(result, dict):
                return self._normalize_plan(result, user_message)
        except Exception as e:
            logger.warning("Universal chat planner failed, falling back to heuristics: %s", e)

        return self._heuristic_plan(user_message)

    def _normalize_plan(self, plan: Dict[str, Any], user_message: str) -> Dict[str, Any]:
        fallback_query_type = str(self._stage_settings("query_planner").get("fallback_query_type") or "pipeline_search").lower()
        query_type = str(plan.get("query_type") or fallback_query_type).lower()
        if query_type not in QUERY_TYPES:
            query_type = fallback_query_type if fallback_query_type in QUERY_TYPES else "pipeline_search"

        planner_settings = self._stage_settings("query_planner")
        max_deal_limit = max(int(planner_settings.get("max_deal_limit") or planner_settings.get("default_deal_limit") or 8), 3)
        max_chunks_per_deal = max(int(planner_settings.get("max_chunks_per_deal") or planner_settings.get("default_chunks_per_deal") or 4), 1)
        normalized = {
            "query_type": query_type,
            "deal_filters": {},
            "exact_terms": self._normalize_string_list(plan.get("exact_terms")),
            "keywords": self._normalize_string_list(plan.get("keywords")),
            "metric_terms": self._normalize_string_list(plan.get("metric_terms")),
            "rag_queries": self._normalize_string_list(plan.get("rag_queries")),
            "needs_stats": bool(plan.get("needs_stats")),
            "deal_limit": min(max(int(plan.get("deal_limit") or planner_settings.get("default_deal_limit") or 8), 3), max_deal_limit),
            "chunks_per_deal": min(max(int(plan.get("chunks_per_deal") or planner_settings.get("default_chunks_per_deal") or 2), 1), max_chunks_per_deal),
            "user_query": user_message,
        }

        for field in ["title", "industry", "sector", "city", "priority", "current_phase", "is_female_led", "management_meeting"]:
            value = (plan.get("deal_filters") or {}).get(field)
            if value not in [None, "", "null", "None"]:
                normalized["deal_filters"][field] = value

        if not normalized["rag_queries"]:
            normalized["rag_queries"] = [user_message]
        if not normalized["keywords"]:
            normalized["keywords"] = self._tokenize_keywords(user_message)
        if not normalized["metric_terms"]:
            normalized["metric_terms"] = [term for term in normalized["keywords"] if term.lower() in METRIC_TOKENS]
        return normalized

    def _heuristic_plan(self, user_message: str) -> Dict[str, Any]:
        lowered = user_message.lower()
        query_type = "stats" if any(word in lowered for word in ["count", "how many", "total"]) else "pipeline_search"
        if any(word in lowered for word in ["compare", "versus", "vs", "difference"]):
            query_type = "comparison"
        if any(word in lowered for word in ["timeline", "phase", "status", "stage"]):
            query_type = "timeline"

        exact_terms = re.findall(r'"([^"]+)"', user_message)
        keywords = self._tokenize_keywords(user_message)
        metric_terms = [term for term in keywords if term.lower() in METRIC_TOKENS or term.upper() in {"ARR", "MRR", "CM1", "CM2"}]
        deal_filters: Dict[str, Any] = {}

        if "female led" in lowered:
            deal_filters["is_female_led"] = True
        if "management meeting" in lowered or "management met" in lowered:
            deal_filters["management_meeting"] = True
        for token in STAGE_TOKENS:
            if token in lowered:
                deal_filters["current_phase"] = token
                break

        planner_settings = self._stage_settings("query_planner")
        return {
            "query_type": query_type,
            "deal_filters": deal_filters,
            "exact_terms": exact_terms,
            "keywords": keywords,
            "metric_terms": metric_terms,
            "rag_queries": [user_message],
            "needs_stats": query_type == "stats" and self._stage_enabled("stats_block"),
            "deal_limit": int(planner_settings.get("default_deal_limit") or 8),
            "chunks_per_deal": int(planner_settings.get("default_chunks_per_deal") or 2),
            "user_query": user_message,
        }

    def _get_candidate_deals(self, plan: Dict[str, Any]) -> List[Deal]:
        filter_settings = self._stage_settings("deal_filtering")
        rerank_settings = self._stage_settings("chunk_rerank")
        queryset = Deal.objects.all().prefetch_related("phase_logs")
        filters = plan.get("deal_filters", {})

        if "is_female_led" in filters:
            queryset = queryset.filter(is_female_led=filters["is_female_led"])
        if "management_meeting" in filters:
            queryset = queryset.filter(management_meeting=filters["management_meeting"])
        for field in ["title", "industry", "sector", "city", "priority", "current_phase"]:
            value = filters.get(field)
            if value:
                queryset = queryset.filter(**{f"{field}__icontains": str(value)})

        semantic_query = " | ".join(plan.get("rag_queries") or [plan["user_query"]])
        semantic_limit = int(filter_settings.get("result_limit") or plan.get("deal_limit") or 20)
        semantic_matches = self.embed_service.search_deal_profiles(
            semantic_query,
            limit=semantic_limit,
            filters=filters,
        )
        pool = list(queryset.order_by("-created_at")[: int(filter_settings.get("candidate_pool_limit") or 60)])
        if not pool:
            return semantic_matches[: plan.get("deal_limit", 8)] if semantic_matches else []

        scored = []
        for deal in pool:
            haystacks = [
                deal.title or "",
                deal.industry or "",
                deal.sector or "",
                deal.city or "",
                deal.deal_summary or "",
                " ".join(deal.themes if isinstance(deal.themes, list) else []),
            ]
            combined = " ".join(haystacks).lower()
            score = 0

            for phrase in plan.get("exact_terms", []):
                phrase_lower = phrase.lower()
                if phrase_lower in (deal.title or "").lower():
                    score += float(rerank_settings.get("deal_title_exact_boost") or 100)
                if phrase_lower in combined:
                    score += float(rerank_settings.get("deal_context_exact_boost") or 40)

            for keyword in plan.get("keywords", []):
                keyword_lower = keyword.lower()
                if keyword_lower in (deal.title or "").lower():
                    score += float(rerank_settings.get("deal_title_keyword_boost") or 30)
                if keyword_lower in combined:
                    score += float(rerank_settings.get("deal_context_keyword_boost") or 10)
                if keyword_lower in " ".join(deal.themes if isinstance(deal.themes, list) else []).lower():
                    score += float(rerank_settings.get("deal_context_keyword_boost") or 10) * 1.5

            for metric in plan.get("metric_terms", []):
                if metric.lower() in combined:
                    score += float(rerank_settings.get("deal_metric_boost") or 20)

            semantic_rank = next((idx for idx, semantic_deal in enumerate(semantic_matches) if semantic_deal.id == deal.id), None)
            if semantic_rank is not None:
                score += max(0, semantic_limit - semantic_rank) * 12

            scored.append((score, deal))

        scored.sort(key=lambda item: (item[0], item[1].created_at), reverse=True)
        top = [deal for score, deal in scored if score > 0][: plan.get("deal_limit", 8)]
        if top:
            return top
        if semantic_matches:
            return semantic_matches[: plan.get("deal_limit", 8)]
        return pool[: min(plan.get("deal_limit", 8), int(filter_settings.get("result_limit") or plan.get("deal_limit", 8)))]

    def _search_ranked_chunks(self, plan: Dict[str, Any], deals: List[Deal]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        retrieval_settings = self._stage_settings("chunk_retrieval")
        rerank_settings = self._stage_settings("chunk_rerank")
        rag_query = " | ".join(plan.get("rag_queries") or [plan["user_query"]])
        exact_terms = [term.lower() for term in plan.get("exact_terms", [])]
        keywords = [term.lower() for term in plan.get("keywords", [])]
        metric_terms = [term.lower() for term in plan.get("metric_terms", [])]
        scored_items: List[Dict[str, Any]] = []
        dropped_by_zero_score = 0

        deal_ids = [str(deal.id) for deal in deals]
        candidate_limit = int(retrieval_settings.get("vector_limit") or 60)
        candidate_chunks = self.embed_service.search_global_chunks(
            rag_query,
            limit=candidate_limit,
            deal_ids=deal_ids or None,
        )
        if not candidate_chunks:
            queryset = DocumentChunk.objects.all().select_related("deal")
            if deals:
                queryset = queryset.filter(deal__in=deals)
            candidate_chunks = list(queryset.order_by("-created_at")[: int(retrieval_settings.get("fallback_candidate_limit") or 120)])

        for chunk in candidate_chunks:
            content_lower = (chunk.content or "").lower()
            title_lower = str((chunk.metadata or {}).get("title", "")).lower()
            score = 0.0

            rerank_score = getattr(chunk, "rerank_score", None)
            if rerank_score is not None:
                score += float(rerank_score) * 100

            distance = getattr(chunk, "distance", None)
            if distance is not None:
                score += max(0.0, 1.0 - float(distance)) * 100

            for term in exact_terms:
                if term in title_lower:
                    score += float(rerank_settings.get("chunk_title_exact_boost") or 120)
                if term in content_lower:
                    score += float(rerank_settings.get("chunk_content_exact_boost") or 60)

            for term in metric_terms:
                if term in content_lower:
                    score += float(rerank_settings.get("chunk_metric_boost") or 50)

            for term in keywords:
                if term in title_lower:
                    score += float(rerank_settings.get("chunk_title_keyword_boost") or 25)
                if term in content_lower:
                    score += float(rerank_settings.get("chunk_content_keyword_boost") or 12)

            if plan.get("query_type") == "timeline" and any(token in content_lower for token in ["phase", "stage", "meeting", "ic", "timeline"]):
                score += float(rerank_settings.get("timeline_bonus") or 25)

            if score <= 0:
                dropped_by_zero_score += 1
                continue

            scored_items.append({"chunk": chunk, "score": round(score, 3)})

        scored_items.sort(key=lambda item: item["score"], reverse=True)
        selected: List[Dict[str, Any]] = []
        per_deal_counts: Dict[str, int] = {}
        seen_keys = set()
        max_per_deal, max_total = self._compute_chunk_budgets(plan, deals)
        dropped_by_per_deal_cap = 0
        dropped_by_total_cap = 0
        dropped_as_duplicates = 0

        for item in scored_items:
            chunk = item["chunk"]
            deal_key = str(chunk.deal_id)
            metadata = chunk.metadata or {}
            chunk_key = (deal_key, chunk.source_id, metadata.get("chunk_index"))
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
            "selected_sources": [
                f"{item['chunk'].deal.title}|{((item['chunk'].metadata or {}).get('title') or (item['chunk'].metadata or {}).get('filename') or item['chunk'].source_type)}"
                for item in selected
            ],
        }
        return selected, diagnostics

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
            "summary_excerpt": ((canonical_snapshot or {}).get("analyst_report") or deal.deal_summary or "")[: int(self._stage_settings("context_assembly").get("deal_summary_excerpt_chars", 900) or 900)],
            "current_analysis": current_analysis,
            "recent_timeline": recent_timeline,
        }

    def _serialize_chunk(self, item: Dict[str, Any]) -> Dict[str, Any]:
        chunk = item["chunk"]
        excerpt = chunk.content[: int(self._stage_settings("context_assembly").get("chunk_excerpt_chars", 1400) or 1400)]
        metadata = chunk.metadata or {}
        return {
            "deal": chunk.deal.title,
            "deal_id": str(chunk.deal_id),
            "source_type": chunk.source_type,
            "source_id": chunk.source_id,
            "source_title": metadata.get("title") or metadata.get("filename"),
            "score": item["score"],
            "text": excerpt,
            "metadata": metadata,
            "document_metadata": self._document_metadata_for_chunk(chunk),
        }

    def _format_context_data(self, plan: Dict[str, Any], deals: List[Dict[str, Any]], chunks: List[Dict[str, Any]], diagnostics: Dict[str, Any] | None = None) -> Tuple[str, Dict[str, Any]]:
        diagnostics = diagnostics or {}
        sections = [
            "[PIPELINE OVERVIEW]",
            self._build_pipeline_overview_from_payload(deals),
            "",
            "[QUERY PLAN]",
            json.dumps(plan, default=str, indent=2),
            "",
            "[CANDIDATE DEALS]",
        ]

        if deals:
            for deal in deals:
                sections.append(
                    f"- {deal['title']} | Industry: {deal.get('industry') or 'N/A'} | "
                    f"Sector: {deal.get('sector') or 'N/A'} | Priority: {deal.get('priority') or 'N/A'} | "
                    f"Phase: {deal.get('current_phase') or 'N/A'} | Themes: {', '.join(deal.get('themes') or []) or 'N/A'}"
                )
                if deal.get("summary_excerpt"):
                    sections.append(f"  Summary: {deal['summary_excerpt']}")
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
        for token in tokens:
            lowered = token.lower()
            if len(lowered) < 3 and lowered.upper() not in {"IC", "CM1", "CM2"}:
                continue
            if lowered in {"what", "which", "with", "from", "that", "this", "have", "show", "about"}:
                continue
            cleaned.append(token)
        return cleaned[:20]

    def _stage_settings(self, stage_id: str) -> Dict[str, Any]:
        return UniversalChatFlowService.stage_settings(self.flow_config, stage_id)

    def _stage_enabled(self, stage_id: str) -> bool:
        for stage in self.flow_config.get("stages", []):
            if stage.get("id") == stage_id:
                return bool(stage.get("enabled", True))
        return False

    def simulate_query(self, user_message: str, conversation_id: str = "admin-preview") -> Dict[str, Any]:
        plan = self._build_query_plan(user_message, conversation_id)
        deals = self._get_candidate_deals(plan)
        chunks, chunk_diagnostics = self._search_ranked_chunks(plan, deals)
        serialized_deals = [self._serialize_deal(deal) for deal in deals]
        serialized_chunks = [self._serialize_chunk(item) for item in chunks]
        context_preview, context_diagnostics = self._format_context_data(plan, serialized_deals, serialized_chunks, diagnostics=chunk_diagnostics)
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
                "deals_selected": len(serialized_deals),
            },
            "answer_prompt_preview": self._stage_settings("answer_generation").get("prompt_template"),
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
        if deal_count == 1:
            effective_per_deal += single_deal_boost
        elif deal_count <= few_deal_threshold:
            effective_per_deal += few_deal_boost
        effective_per_deal = min(effective_per_deal, max_per_deal)

        soft_total = max(int(assembly_settings.get("soft_max_total_chunks") or effective_per_deal * deal_count), effective_per_deal)
        hard_total = max(int(assembly_settings.get("max_total_chunks") or soft_total), effective_per_deal)
        fallback_total = max(int(assembly_settings.get("fallback_max_total_chunks") or hard_total), effective_per_deal)
        if deal_count == 1:
            effective_total = min(hard_total, max(effective_per_deal, soft_total))
        else:
            effective_total = min(hard_total, max(effective_per_deal * deal_count, fallback_total, soft_total))
        return effective_per_deal, effective_total

    def _trim_sections_to_budget(self, sections: List[str]) -> Tuple[List[str], int]:
        assembly_settings = self._stage_settings("context_assembly")
        max_context_chars = int(assembly_settings.get("max_context_chars", 60000) or 60000)
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
