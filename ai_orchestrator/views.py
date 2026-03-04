import logging
import json
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from drf_spectacular.utils import extend_schema, OpenApiParameter
from drf_spectacular.types import OpenApiTypes
from .models import AIPersonality, AISkill
from .services.ai_processor import AIProcessorService
from .services.embedding_processor import EmbeddingService
from deals.models import Deal

logger = logging.getLogger(__name__)

class DealChatView(APIView):
    """
    View to chat with the AI about a specific deal, using all associated emails as context.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Chat about a specific deal",
        description="Assembles all deal data and email extracts into a 32k context window for Mistral Nemo.",
        tags=["Deal Chat"],
        parameters=[
            OpenApiParameter(name='deal_id', type=OpenApiTypes.UUID, location=OpenApiParameter.QUERY, required=True),
        ],
    )
    def post(self, request):
        deal_id = request.query_params.get('deal_id')
        user_message = request.data.get('message')
        chat_history = request.data.get('history', []) # Array of {role: 'user', content: '...'}

        if not deal_id or not user_message:
            return Response({"error": "deal_id and message are required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            deal = Deal.objects.get(id=deal_id)
            
            # 0. STRUCTURED CONTEXT: Get the actual DB record as a dictionary
            # We exclude large text fields that will be covered by RAG chunks
            from django.forms.models import model_to_dict
            structured_data = model_to_dict(deal, exclude=['deal_summary', 'deal_details', 'comments', 'extracted_text'])
            structured_data_str = json.dumps(structured_data, default=str)

            # 1. RETRIEVAL PHASE: Fetch relevant chunks from Vector DB
            print(f"[DEAL CHAT] Querying Vector Store for relevant context...")
            embed_service = EmbeddingService()
            chunks = embed_service.search_similar_chunks(user_message, deal, limit=8)
            
            # 2. Assemble context from chunks
            rag_context = f"CURRENT DEAL DATABASE RECORD: {structured_data_str}\n\nDOCUMENT CHUNKS (Most Relevant):\n"
            data_points = []
            
            if chunks:
                for chunk in chunks:
                    source_name = chunk.metadata.get('filename', chunk.metadata.get('subject', chunk.source_type))
                    rag_context += f"\n--- SOURCE: {source_name} ---\n{chunk.content}\n"
                    if source_name not in data_points:
                        data_points.append(source_name)
            else:
                # FALLBACK: If no vectors exist yet, use the old basic context
                print(f"[DEAL CHAT] WARNING: No vector chunks found. Falling back to basic deal info.")
                rag_context = f"DEAL: {deal.title}\nSECTOR: {deal.sector}\nASK: {deal.funding_ask}\nSUMMARY: {deal.deal_summary}\n"

            # 3. Call AI with retrieved context
            ai_service = AIProcessorService()
            result = ai_service.process_content(
                content=user_message,
                personality_name="default",
                skill_name="deal_chat",
                metadata={'deal_context': rag_context},
                source_id=str(deal.id),
                source_type="deal_chat"
            )

            # Add source attribution to the response
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
    Pass 1: Extract intent/filters.
    Pass 2: Execute Django ORM query.
    Pass 3: Synthesize conversational answer.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user_message = request.data.get('message')
        history = request.data.get('history', []) # Array of {role: 'user', content: '...'}
        
        if not user_message:
            return Response({"error": "message is required"}, status=status.HTTP_400_BAD_REQUEST)

        ai_service = AIProcessorService()

        try:
            import sys
            
            # Format history for the prompt
            history_context = ""
            for msg in history[-5:]: # Last 5 messages for context
                role = "User" if msg.get('role') == 'user' else "Assistant"
                history_context += f"{role}: {msg.get('content')}\n"

            # --- PASS 1: Advanced Intent Extraction ---
            print(f"\n[UNIVERSAL CHAT] PASS 1: Extracting Intent...", flush=True)
            deal_fields = [f.name for f in Deal._meta.get_fields() if not f.is_relation]
            
            pass1_content = f"""USER MESSAGE: {user_message}
CHAT HISTORY: {history_context}

TASK: Parse intent into tools.
AVAILABLE FIELDS (DB): {', '.join(deal_fields)}

TOOLS:
1. "db_filters": Search structured data (e.g. Sector='Fintech').
2. "global_rag": Search document text (e.g. 'ESG strategy', 'patents').
3. "get_stats": User wants counts/summary of pipeline.

Return JSON:
{{
  "db_filters": {{ "sector": "...", "limit": 10 }},
  "global_rag": "query string if needed",
  "get_stats": true/false
}}"""

            intent_result = ai_service.process_content(
                content=pass1_content,
                skill_name="universal_chat",
                source_type="universal_chat_intent"
            )
            
            # --- PASS 2: Multi-Source Execution ---
            context_data = {}
            
            # Tool A: DB Search
            db_filters = intent_result.get("db_filters", {})
            if db_filters:
                from django.db.models import Q
                q_obj = Q()
                for f, v in db_filters.items():
                    if f in deal_fields and v: q_obj &= Q(**{f"{f}__icontains": v})
                
                deals = Deal.objects.filter(q_obj).order_by('-created_at')[:10]
                context_data["database_results"] = [
                    {"title": d.title, "sector": d.sector, "priority": d.priority, "summary": d.deal_summary[:100]} 
                    for d in deals
                ]

            # Tool B: Global Semantic Search
            rag_query = intent_result.get("global_rag")
            if rag_query:
                embed_service = EmbeddingService()
                chunks = embed_service.search_global_chunks(rag_query, limit=10)
                context_data["document_insights"] = [
                    {"deal": c.deal.title, "text": c.content, "source": c.source_type} 
                    for c in chunks
                ]

            # Tool C: Analytics Summary
            if intent_result.get("get_stats"):
                from django.db.models import Count
                stats = Deal.objects.values('sector', 'priority').annotate(count=Count('id'))
                context_data["pipeline_stats"] = list(stats)

            # --- PASS 3: Synthesis ---
            print(f"[UNIVERSAL CHAT] PASS 3: Synthesizing final answer...", flush=True)
            context_payload = f"""SYSTEM CONTEXT: {json.dumps(context_data)}
CHAT HISTORY: {history_context}
USER MESSAGE: {user_message}

INSTRUCTIONS:
1. Use the provided SYSTEM CONTEXT to answer.
2. If document_insights are present, mention which deals they come from.
3. If pipeline_stats are present, provide a high-level overview.
4. If no data is found, be honest and suggest what to search for.
5. Use Markdown (bolding, tables) for readability."""

            final_result = ai_service.process_content(
                content=context_payload,
                skill_name="universal_chat",
                source_type="universal_chat_final"
            )
            return Response(final_result, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"Universal Chat error: {str(e)}", exc_info=True)
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class AISettingsView(APIView):
    """
    View to manage AI Orchestrator settings.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Get current AI settings and available models",
        tags=["AI Settings"],
    )
    def get(self, request):
        try:
            ai_service = AIProcessorService()
            available_models = ai_service.get_available_models()
            
            # Get the default personality
            try:
                personality = AIPersonality.objects.get(is_default=True)
                current_settings = {
                    "personality_id": personality.id,
                    "model_provider": personality.model_provider,
                    "text_model_name": personality.text_model_name,
                    "vision_model_name": personality.vision_model_name,
                    "system_instructions": personality.system_instructions,
                }
            except AIPersonality.DoesNotExist:
                current_settings = {
                    "error": "No default personality found. Please run 'python manage.py seed_ai_prompts'."
                }

            return Response({
                "available_models": available_models,
                "current_settings": current_settings
            }, status=status.HTTP_200_OK)
            
        except Exception as e:
            logger.error(f"Error fetching AI settings: {str(e)}")
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @extend_schema(
        summary="Update default AI model and personality settings",
        tags=["AI Settings"],
    )
    def post(self, request):
        try:
            text_model_name = request.data.get("text_model_name")
            vision_model_name = request.data.get("vision_model_name")
            model_provider = request.data.get("model_provider", "ollama")
            
            if not text_model_name or not vision_model_name:
                return Response({"error": "Both text_model_name and vision_model_name are required"}, status=status.HTTP_400_BAD_REQUEST)

            # Update the default personality
            try:
                personality = AIPersonality.objects.get(is_default=True)
                personality.text_model_name = text_model_name
                personality.vision_model_name = vision_model_name
                personality.model_provider = model_provider
                personality.save()
                
                return Response({
                    "success": True, 
                    "message": f"Updated AI models to {text_model_name} (Text) and {vision_model_name} (Vision)"
                }, status=status.HTTP_200_OK)
            except AIPersonality.DoesNotExist:
                return Response({"error": "No default personality found."}, status=status.HTTP_404_NOT_FOUND)

        except Exception as e:
            logger.error(f"Error updating AI settings: {str(e)}")
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class AISkillsView(APIView):
    """
    View to retrieve all AI skills and their prompt templates.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="List all AI skills",
        description="Returns a list of all configured AI skills, their descriptions, and exact prompt templates.",
        tags=["AI Settings"],
    )
    def get(self, request):
        try:
            skills = AISkill.objects.all().order_by('name')
            data = [
                {
                    "id": str(skill.id),
                    "name": skill.name,
                    "description": skill.description,
                    "prompt_template": skill.prompt_template,
                    "input_schema": skill.input_schema,
                    "output_schema": skill.output_schema
                } for skill in skills
            ]
            return Response(data, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(f"Error fetching AI skills: {str(e)}")
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
