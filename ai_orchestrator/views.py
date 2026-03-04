import logging
import json
import time
from typing import Dict, Any, Optional, List
from django.db.models import Q, Count
from django.forms.models import model_to_dict
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status, viewsets
from drf_spectacular.utils import extend_schema, OpenApiParameter
from drf_spectacular.types import OpenApiTypes

from .models import AIPersonality, AISkill, AIConversation, AIMessage
from .serializers import AIConversationSerializer, AIMessageSerializer
from .services.ai_processor import AIProcessorService
from .services.embedding_processor import EmbeddingService
from deals.models import Deal

logger = logging.getLogger(__name__)

class AIConversationViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing AI chat conversations.
    """
    serializer_class = AIConversationSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return AIConversation.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

class DealChatView(APIView):
    """
    View to chat with the AI about a specific deal, using all associated emails as context.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Chat about a specific deal",
        tags=["Deal Chat"],
        parameters=[
            OpenApiParameter(name='deal_id', type=OpenApiTypes.UUID, location=OpenApiParameter.QUERY, required=True),
        ],
    )
    def post(self, request):
        deal_id = request.query_params.get('deal_id')
        user_message = request.data.get('message')
        chat_history = request.data.get('history', [])

        if not deal_id or not user_message:
            return Response({"error": "deal_id and message are required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            deal = Deal.objects.get(id=deal_id)
            structured_data = model_to_dict(deal, exclude=['deal_summary', 'deal_details', 'comments', 'extracted_text'])
            structured_data_str = json.dumps(structured_data, default=str)

            print(f"[DEAL CHAT] Querying Vector Store for relevant context...")
            embed_service = EmbeddingService()
            chunks = embed_service.search_similar_chunks(user_message, deal, limit=8)
            
            rag_context = f"CURRENT DEAL DATABASE RECORD: {structured_data_str}\n\nDOCUMENT CHUNKS (Most Relevant):\n"
            data_points = []
            
            if chunks:
                for chunk in chunks:
                    source_name = chunk.metadata.get('filename', chunk.metadata.get('subject', chunk.source_type))
                    rag_context += f"\n--- SOURCE: {source_name} ---\n{chunk.content}\n"
                    if source_name not in data_points:
                        data_points.append(source_name)
            else:
                rag_context = f"DEAL: {deal.title}\nSECTOR: {deal.sector}\nASK: {deal.funding_ask}\nSUMMARY: {deal.deal_summary}\n"

            ai_service = AIProcessorService()
            result = ai_service.process_content(
                content=user_message,
                personality_name="default",
                skill_name="deal_chat",
                metadata={'deal_context': rag_context},
                source_id=str(deal.id),
                source_type="deal_chat"
            )

            result["data_points"] = data_points
            return Response(result, status=status.HTTP_200_OK)

        except Deal.DoesNotExist:
            return Response({"error": "Deal not found"}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            logger.error(f"Chat error: {str(e)}", exc_info=True)
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class UniversalChatView(APIView):
    """
    Agentic view to chat with the AI about the entire deal pipeline.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user_message = request.data.get('message')
        history = request.data.get('history', [])
        conversation_id = request.data.get('conversation_id')
        
        if not user_message:
            return Response({"error": "message is required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            # 0. PERSISTENCE
            if conversation_id:
                try:
                    conversation = AIConversation.objects.get(id=conversation_id, user=request.user)
                except (AIConversation.DoesNotExist, ValueError):
                    conversation = AIConversation.objects.create(user=request.user, title=user_message[:50])
            else:
                conversation = AIConversation.objects.create(user=request.user, title=user_message[:50])
            
            AIMessage.objects.create(conversation=conversation, role='user', content=user_message)

            # --- LLM Pipeline ---
            ai_service = AIProcessorService()
            history_context = ""
            for msg in history[-5:]:
                role = "User" if msg.get('role') == 'user' else "Assistant"
                history_context += f"{role}: {msg.get('content')}\n"

            # PASS 1: Intent
            deal_fields = [f.name for f in Deal._meta.get_fields() if not f.is_relation]
            pass1_content = f"USER MESSAGE: {user_message}\nCHAT HISTORY: {history_context}\nTASK: Parse intent into tools.\nAVAILABLE FIELDS: {', '.join(deal_fields)}\nTOOLS: db_filters, global_rag, get_stats. Return JSON."
            
            intent_result = ai_service.process_content(content=pass1_content, skill_name="universal_chat", source_type="universal_chat_intent")
            
            # PASS 2: Multi-Source Execution
            context_data = {}
            
            # 2a. DB Search: Search ALL text fields if filters are provided
            db_filters = intent_result.get("db_filters", {})
            query_set = Deal.objects.all().order_by('-created_at')
            
            if db_filters:
                q_obj = Q()
                for f, v in db_filters.items():
                    if v and v != "null":
                        # If AI gives a generic 'query', search title and summary
                        if f == 'query':
                            q_obj |= Q(title__icontains=v) | Q(deal_summary__icontains=v)
                        elif hasattr(Deal, f):
                            q_obj &= Q(**{f"{f}__icontains": v})
                
                deals = query_set.filter(q_obj)[:15]
            else:
                # FALLBACK: Always provide the 10 most recent deals so AI has context
                deals = query_set[:10]

            context_data["database_results"] = [
                {
                    "title": d.title, 
                    "sector": d.sector, 
                    "industry": d.industry,
                    "funding_ask": d.funding_ask,
                    "priority": d.priority,
                    "summary": d.deal_summary[:200] if d.deal_summary else "No summary"
                } for d in deals
            ]

            # 2b. Global Semantic Search (RAG)
            rag_query = intent_result.get("global_rag")
            if rag_query:
                embed_service = EmbeddingService()
                chunks = embed_service.search_global_chunks(rag_query, limit=10)
                context_data["document_insights"] = [
                    {"deal": c.deal.title, "text": c.content, "source": c.source_type} 
                    for c in chunks
                ]

            if intent_result.get("get_stats"):
                stats = Deal.objects.values('sector', 'priority').annotate(count=Count('id'))
                context_data["pipeline_stats"] = list(stats)

            # PASS 3: Synthesis
            context_payload = f"SYSTEM CONTEXT: {json.dumps(context_data)}\nCHAT HISTORY: {history_context}\nUSER MESSAGE: {user_message}\nINSTRUCTIONS: Answer the user thoroughly in Markdown. Use 'response' key."
            final_result = ai_service.process_content(content=context_payload, skill_name="universal_chat", source_type="universal_chat_final")
            
            if "response" not in final_result:
                final_result["response"] = final_result.get("_raw_response", "Formatting error.")

            # Save Assistant Response
            AIMessage.objects.create(
                conversation=conversation, 
                role='assistant', 
                content=final_result["response"],
                data_points=final_result.get("data_points", []),
                applied_filters=intent_result.get("db_filters", {})
            )
            conversation.save()
            final_result["conversation_id"] = str(conversation.id)

            return Response(final_result, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"Universal Chat error: {str(e)}", exc_info=True)
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class AISettingsView(APIView):
    """View to manage AI Orchestrator settings."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            ai_service = AIProcessorService()
            available_models = ai_service.get_available_models()
            try:
                personality = AIPersonality.objects.get(is_default=True)
                current_settings = {
                    "personality_id": personality.id,
                    "text_model_name": personality.text_model_name,
                    "vision_model_name": personality.vision_model_name,
                }
            except AIPersonality.DoesNotExist:
                current_settings = {"error": "No default personality found."}
            return Response({"available_models": available_models, "current_settings": current_settings}, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def post(self, request):
        try:
            text_model = request.data.get("text_model_name")
            vision_model = request.data.get("vision_model_name")
            personality = AIPersonality.objects.get(is_default=True)
            personality.text_model_name = text_model
            personality.vision_model_name = vision_model
            personality.save()
            return Response({"success": True})
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class AISkillsView(APIView):
    """View to retrieve all AI skills."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            skills = AISkill.objects.all().order_by('name')
            data = [{"id": str(s.id), "name": s.name, "description": s.description} for s in skills]
            return Response(data, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
