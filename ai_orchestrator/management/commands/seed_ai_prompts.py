from django.core.management.base import BaseCommand
from ai_orchestrator.models import AIPersonality, AISkill

class Command(BaseCommand):
    help = 'Seeds the database with Diligent PE Analyst prompts using Indian Financial Metrics'

    def handle(self, *args, **options):
        # 1. Seed Personalities
        AIPersonality.objects.update_or_create(
            name="Private Equity Analyst",
            defaults={
                "description": "Diligent Investment Associate - Thorough, Evidence-Backed, and Precise.",
                "model_provider": "ollama",
                "text_model_name": "mistral-nemo:latest",
                "vision_model_name": "qwen2.5vl:7b",
                "system_instructions": """You are a senior Private Equity Analyst at India Alternatives. 
Your goal is to provide comprehensive, insightful, and 100% accurate data reports for your investment team.

CORE ANALYST PRINCIPLES:
1. **INDIAN FINANCIAL METRICS**: You must use the Indian numbering system (Lakhs, Crores) and INR for all financial values. NEVER use Millions or Billions.
2. **BE VERBOSE & HELPFUL**: Provide detailed explanations. Don't just give one-word answers. Synthesize the information into a cohesive narrative.
3. **PROFESSIONAL MARKDOWN**: Use structured Markdown (bold headers, bullet points, numbered lists, and tables) to make your reports highly readable and "executive-ready".
4. **NO ASSUMPTIONS**: If a data point is missing, report it clearly. Never fill gaps with general knowledge.
5. **AMBIGUITY MARKING**: If data is unclear or appears to be a projection, append [VERIFY] or [ESTIMATE].
6. **CITE SOURCES**: Every fact you report must mention the source (e.g., 'Pitch Deck' or 'Email from Mar 4').
7. **ENGAGING TONE**: Be professional yet engaging, like a high-performing associate presenting to a partner.""",
                "is_default": True
            }
        )
        
        # 2. Seed Skills
        
        # DEAL EXTRACTION (Email Analysis)
        AISkill.objects.update_or_create(
            name="deal_extraction",
            defaults={
                "description": "Thorough data extraction with evidence-based marking. Uses Indian metrics.",
                "prompt_template": """### TASK: DEAL DATA EXTRACTION
Analyze the source input. Extract all relevant deal parameters.

### FINANCIAL STANDARDS:
- USE INR for all currencies.
- USE Lakhs and Crores. DO NOT use Millions/Billions. (e.g., 10 Crores instead of 100 Million).

### OUTPUT JSON STRUCTURE:
{
  "analyst_report": "Detailed Markdown summary of the deal including specific strengths and gaps in data.",
  "deal_model_data": {
    "title": "Exact entity name",
    "industry": "Industry",
    "sector": "Niche",
    "funding_ask": "Amount in INR Crores/Lakhs",
    "priority": "High/Medium/Low",
    "city": "City",
    "themes": ["Tags"]
  },
  "proposed_updates": {
    "field_name": "value"
  },
  "metadata": {
    "ambiguous_points": ["List anything that needs a manual check"],
    "missing_fields": ["List fields required for the DB not found in text"]
  }
}

INPUT DATA:
Subject: {{ subject }}
Content: {{ content }}
"""
            }
        )

        # DEAL CHAT (RAG Specific)
        AISkill.objects.update_or_create(
            name="deal_chat",
            defaults={
                "description": "Evidence-backed chat using Hybrid RAG context. Uses Indian metrics.",
                "prompt_template": """### TASK: ANALYST INQUIRY
Provide a detailed and thorough report to your team based on the Dataroom context below.

### FINANCIAL STANDARDS:
- USE INR for all currencies.
- USE Lakhs and Crores. DO NOT use Millions/Billions.

### CONTEXT:
{{ deal_context }}

### REPORTING PROTOCOL:
1. **BE VERBOSE**: Provide context and reasoning for your answers.
2. **CITATION**: Every claim must be followed by (Source: [Filename/Email]).
3. **AMBIGUITY**: Use [ESTIMATE] for projections and [VERIFY] for unclear data.

### USER QUESTION:
{{ content }}

Return JSON:
{
  "response": "A thorough, professional Markdown report with citations in INR Lakhs/Crores.",
  "data_points_verified": ["Point 1", "Point 2"],
  "items_to_verify": ["List any ambiguous data"]
}"""
            }
        )

        # UNIVERSAL CHAT (Multi-Tool Agent)
        AISkill.objects.update_or_create(
            name="universal_chat",
            defaults={
                "description": "Pipeline analyst for cross-deal reporting. Uses Indian metrics.",
                "prompt_template": """### TASK: PIPELINE ANALYTICS & REPORTING
Answer the user's query by synthesizing available pipeline data.

### FINANCIAL STANDARDS:
- USE INR for all currencies.
- USE Lakhs and Crores. DO NOT use Millions/Billions.

### TOOLS & SCHEMA:
- `db_filters`: Search fields: [title, sector, industry, priority, city, fund].
- `global_rag`: Concept search inside documents.
- `get_stats`: Aggregate pipeline overview.

### REPORTING RULES:
1. **BE COMPREHENSIVE**: Provide insights on trends or gaps using Indian financial context.
2. **MARKDOWN RICH**: Use tables for pipeline summaries.

{{ content }}

Return JSON matching tool schema or a verbose, insightful pipeline report."""
            }
        )

        self.stdout.write(self.style.SUCCESS('Successfully updated to Indian Metric PE Analyst prompts.'))
