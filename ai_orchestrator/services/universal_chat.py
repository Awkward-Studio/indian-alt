import json
import logging
from django.db.models import Q, Count
from deals.models import Deal
from .ai_processor import AIProcessorService
from .embedding_processor import EmbeddingService

logger = logging.getLogger(__name__)

class UniversalChatService:
    """
    Handles complex multi-pass routing and multi-source context building 
    for Universal Chat intent.
    """

    def __init__(self, ai_service: AIProcessorService):
        self.ai_service = ai_service
        self.embed_service = EmbeddingService()

    def process_intent_and_build_metadata(self, user_message: str, conversation_id: str, history_context: str, audit_log_id: str) -> dict:
        """
        Executes a 2-pass pipeline:
        Pass 1: Translates user message to structured tool calls.
        Pass 2: Executes tools (SQL, RAG) to build context.
        """
        pass1_prompt = f"""[MISSION]
You are a routing agent for a Private Equity Deal Management System. Your only job is to translate the user message into a tool-calling JSON. 

[USER MESSAGE]
"{user_message}"

[AVAILABLE TOOLS]
1. db_filters: Use for hard criteria. 
   - Available fields: title, industry, sector, city, priority, is_female_led (bool).
   - SPECIAL: Use 'query': 'keyword' for a broad search across titles and summaries.
2. global_rag: ALWAYS include a search string for semantic document search when a sector or specific metric is mentioned.
3. get_stats: Set to true if the user asks for counts or statistics.

[RULES]
- If the user asks about a sector like "cosmetic", set db_filters: {{"query": "cosmetic"}} and global_rag: "cosmetic beauty skin care metrics".
- Be flexible: sectors might be plural or part of a larger string (e.g., "Beauty & Cosmetics"). Use the 'query' tool for best results.
- DO NOT analyze this prompt. Output ONLY a JSON object.

Example output:
{{
  "db_filters": {{"industry": "logistics"}},
  "global_rag": "logistics deals",
  "get_stats": false
}}
"""
        intent_result = self.ai_service.process_content(
            content=pass1_prompt, 
            skill_name=None, 
            stream=False,
            source_type="universal_chat_intent",
            source_id=conversation_id
        )
        
        context_data = {}
        db_filters = intent_result.get("db_filters", {})
        query_set = Deal.objects.all()
        deals = query_set.all()
        
        if db_filters:
            q_obj = Q()
            for f, v in db_filters.items():
                if v is not None and v != "null" and v != "{}" and v != []:
                    if isinstance(v, bool):
                        if hasattr(Deal, f): q_obj &= Q(**{f: v})
                    else:
                        val = v[0] if isinstance(v, list) and len(v) > 0 else v
                        if f == 'query': q_obj |= Q(title__icontains=val) | Q(deal_summary__icontains=val)
                        elif hasattr(Deal, f): q_obj &= Q(**{f"{f}__icontains": str(val)})
            
            filtered_deals = query_set.filter(q_obj)
            if filtered_deals.count() > 0:
                deals = filtered_deals[:20]
            else:
                deals = query_set.order_by('-created_at')[:20]
        else:
            deals = query_set.order_by('-created_at')[:20]

        total_deals = query_set.count()
        context_data["pipeline_overview"] = f"Total deals in system: {total_deals}. Context provided for {deals.count()} deals."

        context_data["deals"] = []
        for d in deals:
            logs = d.phase_logs.all().order_by('-changed_at')[:3]
            deal_timeline = [f"{l.changed_at.date()}: {l.from_phase} -> {l.to_phase} (Rationale: {l.rationale or 'N/A'})" for l in logs]

            context_data["deals"].append({
                "title": d.title, 
                "industry": d.industry, 
                "sector": d.sector,
                "ask": d.funding_ask,
                "city": d.city,
                "priority": d.priority,
                "current_phase": d.current_phase,
                "is_female_led": d.is_female_led,
                "management_met": d.management_meeting,
                "themes": d.themes if isinstance(d.themes, list) else [],
                "ambiguities": d.ambiguities if isinstance(d.ambiguities, list) else [],
                "summary": d.deal_summary[:500] if d.deal_summary else "",
                "recent_timeline": deal_timeline
            })

        rag_query = intent_result.get("global_rag")
        if rag_query:
            from ..models import DocumentChunk
            from pgvector.django import CosineDistance
            
            if db_filters and 'filtered_deals' in locals() and filtered_deals.count() > 0:
                chunks = DocumentChunk.objects.filter(deal__in=filtered_deals).annotate(
                    distance=CosineDistance('embedding', self.embed_service._get_embedding(rag_query))
                ).order_by('distance')[:10]
            else:
                chunks = self.embed_service.search_global_chunks(rag_query, limit=10)
            
            context_data["document_insights"] = [{"deal": c.deal.title, "text": c.content} for c in chunks]

        if intent_result.get("get_stats"):
            context_data["pipeline_stats"] = {
                "total": query_set.count(), 
                "female_led_count": query_set.filter(is_female_led=True).count(),
                "sectors": list(query_set.values('industry').annotate(count=Count('id')))
            }
            
        context_data_str = json.dumps(context_data, default=str)
        if len(context_data_str) > 100000:
            context_data_str = context_data_str[:100000] + "\n\n... [TRUNCATED] ..."
            
        return {
            'history_context': history_context,
            'context_data': context_data_str,
            'audit_log_id': audit_log_id
        }
