import logging
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from drf_spectacular.utils import extend_schema
from .models import AIPersonality
from .services.ai_processor import AIProcessorService

logger = logging.getLogger(__name__)

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
