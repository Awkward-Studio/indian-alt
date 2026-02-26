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

            # --- PASS 1: Identify Search Filters ---
            print(f"\n[UNIVERSAL CHAT] PASS 1: Extracting Intent...", flush=True)
            
            # STRENGHTENED PROMPT for Pass 1 to prevent hallucination
            pass1_content = f"""CHAT HISTORY:
{history_context}

CURRENT USER MESSAGE: {user_message}

TASK: You are a query parser. 
- Analyze the user message in context of the history.
- If the user says 'these' or 'them', refer to previous assistant messages.
- If the user asks about 'how many' or 'list' deals, you MUST return a search_query with limit: 20.
- Do NOT try to answer the question.
- Return ONLY JSON."""

            intent_result = ai_service.process_content(
                content=pass1_content,
                skill_name="universal_chat",
                source_type="universal_chat_intent"
            )
            
            # DEBUG: What did the AI actually think?
            print(f"[DEBUG] AI Intent Response: {json.dumps(intent_result)}", flush=True)

            search_query = intent_result.get("search_query", {})
            if not isinstance(search_query, dict):
                search_query = {}
                
            deal_data = []

            # --- PASS 2: Execute Django ORM Query ---
            # If the user is asking a general question, we should fetch some deals anyway
            # rather than sending an empty list to the AI.
            queryset = Deal.objects.all()
            has_filters = False
            
            if search_query.get("sector") and search_query.get("sector") != "null":
                queryset = queryset.filter(sector__icontains=search_query["sector"])
                has_filters = True
            if search_query.get("industry") and search_query.get("industry") != "null":
                queryset = queryset.filter(industry__icontains=search_query["industry"])
                has_filters = True
            if search_query.get("priority") and search_query.get("priority") != "null":
                queryset = queryset.filter(priority=search_query["priority"])
                has_filters = True
            
            print(f"[UNIVERSAL CHAT] PASS 2: Executing Query (Filters: {has_filters})...", flush=True)
            
            # Fetch results (either filtered or just the latest 10 for context)
            limit = 20 # Increased limit to ensure we don't miss any
            try:
                limit = int(search_query.get("limit", 20))
            except: pass
            
            deals = queryset.order_by('-created_at')[:limit]
            
            deal_data = []
            for d in deals:
                deal_data.append({
                    "title": d.title or "Untitled Deal",
                    "sector": d.sector,
                    "industry": d.industry,
                    "priority": d.priority,
                    "ask": d.funding_ask,
                    "summary": d.deal_summary[:150] if d.deal_summary else ""
                })
            
            print(f"[UNIVERSAL CHAT] PASS 2: Database returned {len(deal_data)} deals.", flush=True)
            print(f"[DEBUG] Titles in database results: {[d['title'] for d in deal_data]}", flush=True)

            # --- PASS 3: Synthesize Final Answer ---
            print(f"[UNIVERSAL CHAT] PASS 3: Synthesizing final answer...", flush=True)
            
            # Use a more compact dump for logging
            deal_data_json = json.dumps(deal_data)
            
            # IMPORTANT: We inject the context directly into the 'content' because 
            # the prompt template might be failing to resolve the variables properly
            context_payload = f"""DATABASE RESULTS (deal_data): {deal_data_json}

CHAT HISTORY:
{history_context}

CURRENT USER MESSAGE: {user_message}

INSTRUCTIONS: 
1. Use the 'deal_data' and 'CHAT HISTORY' above to answer the user conversationally and intelligently.
2. If the user refers to 'these', 'them', or 'top deals', use the CHAT HISTORY to identify which deals they are talking about.
3. FORMATTING: Use Markdown (bolding, bullet points) to make the response easy to read. 
4. Be professional, clinical, yet approachable—like a Head of Deal Flow.
5. Provide a summary count at the end if applicable."""

            print(f"[DEBUG] Final Prompt sent to AI: {context_payload}", flush=True)
            
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
