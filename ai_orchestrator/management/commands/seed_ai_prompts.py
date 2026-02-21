from django.core.management.base import BaseCommand
from ai_orchestrator.models import AIPersonality, AISkill

class Command(BaseCommand):
    help = 'Seeds the database with default AI personalities and skills'

    def handle(self, *args, **options):
        # 1. Seed Personalities
        # Standard Deal Analyst (Fast, Text-only)
        AIPersonality.objects.update_or_create(
            name="Private Equity MD",
            defaults={
                "description": "Senior decision maker at India Alternatives. Focused on strategic fit and ROI.",
                "model_provider": "ollama",
                "model_name": "llama3.1:latest",
                "system_instructions": """You are an experienced Private Equity Managing Director at India Alternatives. 
Your goal is to quickly assess investment opportunities. Be critical, look for high-level details, and flag potential issues.""",
                "is_default": True
            }
        )
        
        # Multimodal Analyst (Vision-capable)
        AIPersonality.objects.update_or_create(
            name="Visual Deal Analyst",
            defaults={
                "description": "Analyst capable of processing images, charts, and complex documents using Vision models.",
                "model_provider": "ollama",
                "model_name": "llava:latest",
                "system_instructions": """You are a Private Equity Analyst specializing in visual data and complex documents. 
Analyze both the text and any provided images (charts, tables, slides). 
Extract key metrics and strategic insights.""",
                "is_default": False
            }
        )

        # 2. Seed Skills
        AISkill.objects.update_or_create(
            name="deal_extraction",
            defaults={
                "description": "Extracts structured deal information from email content.",
                "prompt_template": """Analyze this Private Equity deal. Extract information and return ONLY JSON.
Metadata:
- From: {{ from_email }}
- Subject: {{ subject }}

JSON Schema:
{
  "deal_name": "string",
  "deal_size": "string",
  "sector": "string",
  "general_summary": "string",
  "potential_red_flags": ["string"]
}"""
            }
        )

        AISkill.objects.update_or_create(
            name="document_analysis",
            defaults={
                "description": "Analyzes extracted text from PDFs, Excels, and PPTs.",
                "prompt_template": """You are provided with text extracted from a deal-related document ({{ filename }}).
Analyze the content and provide a structured summary of the key investment highlights, 
financial metrics, and any risks mentioned. 

Return ONLY JSON:
{
  "document_type": "string",
  "key_highlights": ["string"],
  "financial_metrics": {"metric_name": "value"},
  "identified_risks": ["string"],
  "md_summary": "3-sentence summary"
}"""
            }
        )
        
        self.stdout.write(self.style.SUCCESS('Successfully seeded AI personalities and skills.'))
        self.stdout.write(self.style.WARNING('Note: Ensure you have "llama3.1" and "llava" models pulled in Ollama.'))
