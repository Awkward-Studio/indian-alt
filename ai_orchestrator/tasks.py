import logging
import json
import time
from celery import shared_task
from .models import AIAuditLog, AIMessage, AIConversation, AIPersonality, AISkill
from .services.ai_processor import AIProcessorService

logger = logging.getLogger(__name__)

@shared_task(bind=True)
def generate_chat_response_async(self, conversation_id: str, user_message: str, skill_name: str, metadata: dict, audit_log_id: str):
    """
    Background task to generate and save an AI chat response.
    Ensures the response is persistent even if the user closes the UI.
    """
    try:
        from django.db.models import Q, Count
        from deals.models import Deal
        from .services.embedding_processor import EmbeddingService
        
        conversation = AIConversation.objects.get(id=conversation_id)
        audit_log = AIAuditLog.objects.get(id=audit_log_id)
        
        # Update log with worker info
        audit_log.celery_task_id = self.request.id
        audit_log.status = 'PROCESSING'
        audit_log.save(update_fields=['celery_task_id', 'status'])

        ai_service = AIProcessorService()
        history_context = ""
        
        # --- RETRIEVE CONVERSATION HISTORY ---
        previous_messages = AIMessage.objects.filter(conversation=conversation).order_by('-created_at')[1:11] # Skip the current user message
        for msg in reversed(previous_messages):
            history_context += f"{msg.role.upper()}: {msg.content}\n"
        # -------------------------------------
        
        # --- DYNAMIC CONTEXT BUILDING FOR UNIVERSAL CHAT ---
        if skill_name == 'universal_chat':
            # PASS 1: Intent
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
            intent_result = ai_service.process_content(
                content=pass1_prompt, 
                skill_name=None, 
                stream=False,
                source_type="universal_chat_intent",
                source_id=str(conversation.id)
            )
            
            # PASS 2: Multi-Source Execution
            context_data = {}
            db_filters = intent_result.get("db_filters", {})
            query_set = Deal.objects.all()
            
            deals = query_set.all()
            if db_filters:
                q_obj = Q()
                for f, v in db_filters.items():
                    if v is not None and v != "null" and v != "{}" and v != []:
                        # Handle booleans
                        if isinstance(v, bool):
                            if hasattr(Deal, f): q_obj &= Q(**{f: v})
                        # Handle potential list values from AI
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
                deal_timeline = []
                for l in logs:
                    deal_timeline.append(f"{l.changed_at.date()}: {l.from_phase} -> {l.to_phase} (Rationale: {l.rationale or 'N/A'})")

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
                from .models import DocumentChunk
                from pgvector.django import CosineDistance
                
                embed_service = EmbeddingService()
                if db_filters and 'filtered_deals' in locals() and filtered_deals.count() > 0:
                    chunks = DocumentChunk.objects.filter(deal__in=filtered_deals).annotate(distance=CosineDistance('embedding', embed_service._get_embedding(rag_query))).order_by('distance')[:10]
                else:
                    chunks = embed_service.search_global_chunks(rag_query, limit=10)
                
                context_data["document_insights"] = [{"deal": c.deal.title, "text": c.content} for c in chunks]

            if intent_result.get("get_stats"):
                context_data["pipeline_stats"] = {
                    "total": query_set.count(), 
                    "female_led_count": query_set.filter(is_female_led=True).count(),
                    "sectors": list(query_set.values('industry').annotate(count=Count('id')))
                }
                
            # Prepare final metadata for template injection
            context_data_str = json.dumps(context_data, default=str)
            if len(context_data_str) > 100000:
                context_data_str = context_data_str[:100000] + "\n\n... [TRUNCATED] ..."
                
            task_metadata = {
                'history_context': history_context,
                'context_data': context_data_str,
                'audit_log_id': audit_log_id # Ensure audit log is linked
            }
        else:
            # For other skills (like deal_chat), merge existing metadata with audit_log_id
            task_metadata = metadata or {}
            task_metadata['audit_log_id'] = audit_log_id
        # -----------------------------------------------------

        full_text = ""
        full_thinking = ""
        last_save_time = time.time()

        # Call the AI service with stream=True
        for chunk_str in ai_service.process_content(
            content=user_message,
            skill_name=skill_name,
            source_type=skill_name,
            source_id=str(conversation.id),
            metadata=task_metadata,
            stream=True
        ):
            try:
                chunk = json.loads(chunk_str)
                full_text += chunk.get("response", "")
                full_thinking += chunk.get("thinking", "")
                
                # Throttle DB updates to once per second to avoid lock contention
                if time.time() - last_save_time > 1.0:
                    audit_log.raw_response = full_text
                    audit_log.raw_thinking = full_thinking
                    audit_log.save(update_fields=['raw_response', 'raw_thinking'])
                    last_save_time = time.time()
            except:
                pass

        if full_text:
            # Commit final message to conversation history
            AIMessage.objects.create(
                conversation=conversation,
                role='assistant',
                content=full_text,
                thinking=full_thinking
            )
            # Finalize Audit Log
            audit_log.raw_response = full_text
            audit_log.raw_thinking = full_thinking
            audit_log.status = 'COMPLETED'
            audit_log.is_success = True
            audit_log.save(update_fields=['raw_response', 'raw_thinking', 'status', 'is_success'])
            
            logger.info(f"Background chat response generated for Conv: {conversation_id}")
            return {"status": "success", "message_length": len(full_text)}
        else:
            audit_log.status = 'FAILED'
            audit_log.error_message = "AI returned an empty response."
            audit_log.save()
            return {"status": "error", "error": "Empty response"}

    except Exception as e:
        logger.error(f"Async chat response failed: {str(e)}")
        if 'audit_log' in locals():
            audit_log.status = 'FAILED'
            audit_log.error_message = str(e)
            audit_log.save()
        raise e
