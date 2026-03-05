import logging
import json
import time
import requests
from typing import Dict, Any, Optional, List
from django.db.models import Q, Count
from django.forms.models import model_to_dict
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status, viewsets
from django.http import StreamingHttpResponse

from .models import AIPersonality, AISkill, AIConversation, AIMessage
from .serializers import AIConversationSerializer, AIMessageSerializer
from .services.ai_processor import AIProcessorService
from .services.embedding_processor import EmbeddingService
from .services.vm_service import VMControlService
from deals.models import Deal

logger = logging.getLogger(__name__)

class AIConversationViewSet(viewsets.ModelViewSet):
    serializer_class = AIConversationSerializer
    permission_classes = [IsAuthenticated]
    def get_queryset(self):
        return AIConversation.objects.filter(user=self.request.user)
    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

class VMControlView(APIView):
    permission_classes = [IsAuthenticated]
    def get(self, request):
        vm_service = VMControlService()
        return Response({"status": vm_service.get_status()})
    def post(self, request):
        action = request.data.get('action')
        vm_service = VMControlService()
        success = vm_service.start_vm() if action == 'start' else vm_service.stop_vm()
        return Response({"success": success})

class DealChatView(APIView):
    """
    View to chat with the AI about a specific deal.
    """
    permission_classes = [IsAuthenticated]
    def post(self, request):
        deal_id = request.query_params.get('deal_id')
        user_message = request.data.get('message')
        stream = request.data.get('stream', True)
        if not deal_id or not user_message:
            return Response({"error": "deal_id and message are required"}, status=400)
        try:
            deal = Deal.objects.get(id=deal_id)
            
            # Create a rich, structured representation of the deal's forensic data
            structured_data = {
                "title": deal.title,
                "industry": deal.industry,
                "sector": deal.sector,
                "funding_ask": deal.funding_ask,
                "priority": deal.priority,
                "themes": deal.themes if isinstance(deal.themes, list) else [],
                "ambiguities": deal.ambiguities if isinstance(deal.ambiguities, list) else [],
                "forensic_summary": deal.deal_summary,
                "status_flags": {
                    "female_led": deal.is_female_led,
                    "management_meeting": deal.management_meeting,
                    "proposal_stage": deal.business_proposal_stage,
                    "ic_stage": deal.ic_stage
                }
            }
            
            embed_service = EmbeddingService()
            chunks = embed_service.search_similar_chunks(user_message, deal, limit=8)
            
            rag_context = f"DEAL FORENSIC RECORD:\n{json.dumps(structured_data, default=str, indent=2)}\n\nRAW DOCUMENT CHUNKS (MOST RELEVANT TO QUERY):\n"
            if chunks:
                for chunk in chunks:
                    rag_context += f"\n--- {chunk.metadata.get('filename', 'Source')} ---\n{chunk.content}\n"
            else:
                rag_context += "No specific raw document chunks matched this query."
            
            ai_service = AIProcessorService()
            result = ai_service.process_content(
                content=user_message,
                skill_name="deal_chat",
                metadata={'deal_context': rag_context},
                source_id=str(deal.id),
                source_type="deal_chat",
                stream=stream
            )
            if stream: return StreamingHttpResponse(result, content_type='text/event-stream')
            return Response({"response": result.get("_raw_response", "")})
        except Exception as e:
            logger.error(f"Deal Chat error: {str(e)}", exc_info=True)
            return Response({"error": str(e)}, status=500)

class UniversalChatView(APIView):
    permission_classes = [IsAuthenticated]
    def post(self, request):
        user_message = request.data.get('message')
        history = request.data.get('history', [])
        conversation_id = request.data.get('conversation_id')
        stream = request.data.get('stream', True)
        if not user_message: return Response({"error": "message is required"}, status=400)

        try:
            if conversation_id:
                try: 
                    conversation = AIConversation.objects.get(id=conversation_id, user=request.user)
                except: 
                    conversation = AIConversation.objects.create(user=request.user, title=user_message[:50])
            else:
                conversation = AIConversation.objects.create(user=request.user, title=user_message[:50])
            
            AIMessage.objects.create(conversation=conversation, role='user', content=user_message)
            ai_service = AIProcessorService()
            history_context = ""
            for msg in history[-5:]:
                role = "User" if msg.get('role') == 'user' else "Assistant"
                history_context += f"{role}: {msg.get('content')}\n"

            # PASS 1: Intent
            pass1_prompt = f"""[SYSTEM] Determine tools for: "{user_message}"
DATABASE FIELDS (Use for structured filtering):
- title, industry, sector, city, state, country (text icontains)
- priority (Exact: New, High, Medium, Low)
- is_female_led (bool)
- management_meeting (bool)
- business_proposal_stage, ic_stage (bool)
- funding_ask (Use numeric filters only if sure)

TOOLS: 
- db_filters: {{'industry': 'Logistics', 'is_female_led': true}}
- global_rag: "Specific semantic search for document text (e.g. CM1 margins, revenue run-rate, risk factors)"
- get_stats: true/false

RULES: 
1. Use db_filters for hard criteria (Industry, Stages, Female Led).
2. Use global_rag for deep metrics (Margins, CM1, etc.) or qualitative traits.
3. You can use BOTH tools simultaneously for maximum precision.
4. Return ONLY JSON.
"""
            intent_result = ai_service.process_content(content=pass1_prompt, skill_name=None, stream=False)
            print(f"[AGENT] Intent: {intent_result}")

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
                # Fallback if strict filters are TOO specific
                if filtered_deals.count() > 0:
                    deals = filtered_deals[:50]
                else:
                    print(f"[AGENT] Strict filters {db_filters} returned 0. Using recent deals.")
                    deals = query_set.order_by('-created_at')[:50]
            else:
                deals = query_set.order_by('-created_at')[:50]

            # Provide a complete summary of pipeline stats
            total_deals = query_set.count()
            context_data["pipeline_overview"] = f"Total deals in system: {total_deals}. Context provided for {deals.count()} deals."

            # EXPOSE RICH FORENSIC DATA
            context_data["deals"] = [{
                "title": d.title, 
                "industry": d.industry, 
                "sector": d.sector,
                "ask": d.funding_ask,
                "city": d.city,
                "priority": d.priority,
                "is_female_led": d.is_female_led,
                "management_met": d.management_meeting,
                "themes": d.themes if isinstance(d.themes, list) else [],
                "ambiguities": d.ambiguities if isinstance(d.ambiguities, list) else [],
                "summary": d.deal_summary[:1000] if d.deal_summary else ""
            } for d in deals]

            rag_query = intent_result.get("global_rag")
            if rag_query:
                embed_service = EmbeddingService()
                # If we have specific deals filtered, prioritize chunks from those deals
                if db_filters and filtered_deals.count() > 0:
                    chunks = DocumentChunk.objects.filter(deal__in=filtered_deals).annotate(distance=CosineDistance('embedding', embed_service._get_embedding(rag_query))).order_by('distance')[:15]
                else:
                    chunks = embed_service.search_global_chunks(rag_query, limit=15)
                
                context_data["document_insights"] = [{"deal": c.deal.title, "text": c.content} for c in chunks]

            if intent_result.get("get_stats"):
                context_data["pipeline_stats"] = {
                    "total": query_set.count(), 
                    "female_led_count": query_set.filter(is_female_led=True).count(),
                    "sectors": list(query_set.values('industry').annotate(count=Count('id')))
                }

            # PASS 3: Synthesis
            synthesis_prompt = f"""[CONTEXT]
CHAT HISTORY:
{history_context}

REAL DATA:
{json.dumps(context_data, indent=2, default=str)}

[TASK]
Analyze the user message: "{user_message}"

INSTRUCTIONS:
1. Use the REAL DATA provided. Cross-reference structured fields (like is_female_led) with the document_insights (raw text).
2. If the user asks for prioritization, rank deals based on metrics found in document_insights (CM1, revenue) and the stored ambiguities.
3. Be professional and forensic. Explain WHY a deal is prioritized.
4. If data is missing, state it clearly based on the summary and insights provided.
"""
            if stream:
                def stream_and_save():
                    full_text = ""
                    full_thinking = ""
                    # First chunk contains the conversation ID for the frontend to save
                    yield json.dumps({"conversation_id": str(conversation.id)}) + "\n"
                    
                    for chunk_str in ai_service.process_content(content=synthesis_prompt, skill_name=None, stream=True):
                        try:
                            chunk = json.loads(chunk_str)
                            full_text += chunk.get("response", "")
                            full_thinking += chunk.get("thinking", "")
                        except:
                            pass
                        yield chunk_str
                    
                    AIMessage.objects.create(
                        conversation=conversation, 
                        role='assistant', 
                        content=full_text,
                        thinking=full_thinking
                    )
                    conversation.save()
                return StreamingHttpResponse(stream_and_save(), content_type='text/event-stream')
            
            final_result = ai_service.process_content(content=synthesis_prompt, skill_name=None, stream=False)
            raw_content = final_result.get("_raw_response", "")
            thinking = final_result.get("thinking", "")
            
            AIMessage.objects.create(
                conversation=conversation, 
                role='assistant', 
                content=raw_content,
                thinking=thinking
            )
            conversation.save()
            return Response({
                "response": raw_content,
                "thinking": thinking,
                "conversation_id": str(conversation.id)
            })
        except Exception as e:
            logger.error(f"Universal Chat error: {str(e)}", exc_info=True)
            return Response({"error": str(e)}, status=500)
        except Exception as e:
            logger.error(f"Universal Chat error: {str(e)}", exc_info=True)
            return Response({"error": str(e)}, status=500)

class AISettingsView(APIView):
    permission_classes = [IsAuthenticated]
    def get(self, request):
        try:
            ai_service = AIProcessorService()
            vm_service = VMControlService()
            available_models = ai_service.get_available_models()
            try:
                personality = AIPersonality.objects.get(is_default=True)
                current_settings = {"text_model_name": personality.text_model_name, "vision_model_name": personality.vision_model_name, "model_provider": personality.model_provider}
            except: current_settings = None
            telemetry = {"loaded_models": []}
            try:
                ps_resp = requests.get(f"{ai_service.ollama_url}/api/ps", timeout=2)
                if ps_resp.status_code == 200:
                    for model in ps_resp.json().get('models', []):
                        vram_gb = model.get('size_vram', 0) / 1e9
                        total_size_gb = model.get('size', 0) / 1e9
                        telemetry["loaded_models"].append({"name": model.get('name'), "vram_gb": round(vram_gb, 2), "gpu_percent": round(min((vram_gb/total_size_gb)*100 if total_size_gb > 0 else 0, 100), 1)})
            except: pass
            return Response({"available_models": available_models, "current_settings": current_settings, "telemetry": telemetry, "vm_status": vm_service.get_status()})
        except Exception as e: return Response({"error": str(e)}, status=500)

    def post(self, request):
        try:
            personality = AIPersonality.objects.get(is_default=True)
            personality.text_model_name = request.data.get("text_model_name", personality.text_model_name)
            personality.vision_model_name = request.data.get("vision_model_name", personality.vision_model_name)
            personality.model_provider = request.data.get("model_provider", personality.model_provider)
            personality.save()
            return Response({"success": True})
        except Exception as e: return Response({"error": str(e)}, status=500)

class AISkillsView(APIView):
    permission_classes = [IsAuthenticated]
    def get(self, request):
        try:
            skills = AISkill.objects.all().order_by('name')
            return Response([{"id": str(s.id), "name": s.name, "description": s.description, "prompt_template": s.prompt_template} for s in skills])
        except Exception as e: return Response({"error": str(e)}, status=500)
