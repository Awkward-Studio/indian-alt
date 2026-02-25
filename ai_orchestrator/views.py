import logging
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from drf_spectacular.utils import extend_schema, OpenApiParameter
from drf_spectacular.types import OpenApiTypes
from .models import AIPersonality
from .services.ai_processor import AIProcessorService
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
            deal = Deal.objects.prefetch_related('emails').get(id=deal_id)
            
            # 1. Assemble Deal Context
            deal_info = f"DEAL: {deal.title}\nSECTOR: {deal.sector}\nASK: {deal.funding_ask}\nSUMMARY: {deal.deal_summary}\n"
            
            # 2. Add all extracted email text
            email_context = ""
            for email in deal.emails.all():
                if email.extracted_text:
                    email_context += f"\n--- EMAIL FROM {email.from_email} (Date: {email.date_received}) ---\n"
                    email_context += email.extracted_text + "\n"

            # 3. Build full prompt
            full_context = f"{deal_info}\n{email_context}"
            
            # 4. Call AI with Deal context
            ai_service = AIProcessorService()
            result = ai_service.process_content(
                content=user_message,
                personality_name="default", # Using MD personality
                skill_name="deal_chat", # New skill we will seed
                metadata={'deal_context': full_context},
                source_id=str(deal.id),
                source_type="deal_chat"
            )

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
        if not user_message:
            return Response({"error": "message is required"}, status=status.HTTP_400_BAD_REQUEST)

        ai_service = AIProcessorService()

        try:
            # --- PASS 1: Identify Search Filters ---
            intent_result = ai_service.process_content(
                content=f"USER MESSAGE: {user_message}\n\nTask: Identify search filters. If no filters needed, return empty search_query.",
                skill_name="universal_chat",
                source_type="universal_chat_intent"
            )

            search_query = intent_result.get("search_query", {})
            deal_data = []

            # --- PASS 2: Execute Django ORM Query ---
            if any(search_query.values()):
                queryset = Deal.objects.all()
                
                if search_query.get("sector"):
                    queryset = queryset.filter(sector__icontains=search_query["sector"])
                if search_query.get("industry"):
                    queryset = queryset.filter(industry__icontains=search_query["industry"])
                if search_query.get("priority"):
                    queryset = queryset.filter(priority=search_query["priority"])
                
                # Fetch limited results to keep context focused
                limit = search_query.get("limit", 10)
                deals = queryset.order_by('-created_at')[:limit]
                
                for d in deals:
                    deal_data.append({
                        "id": str(d.id),
                        "title": d.title,
                        "sector": d.sector,
                        "priority": d.priority,
                        "ask": d.funding_ask,
                        "summary": d.deal_summary[:200]
                    })

            # --- PASS 3: Synthesize Final Answer ---
            context_payload = f"USER MESSAGE: {user_message}\n\nDATABASE RESULTS (deal_data): {json.dumps(deal_data)}"
            final_result = ai_service.process_content(
                content=context_payload,
                skill_name="universal_chat",
                source_type="universal_chat_final"
            )

            # Inject search query for frontend transparency
            final_result["applied_filters"] = search_query

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
