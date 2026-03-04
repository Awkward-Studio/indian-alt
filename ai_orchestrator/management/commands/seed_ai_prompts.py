from django.core.management.base import BaseCommand
from ai_orchestrator.models import AIPersonality, AISkill

class Command(BaseCommand):
    help = 'Seeds the database with Diligent PE Analyst prompts (High-Thoroughness, Anti-Hallucination)'

    def handle(self, *args, **options):
        # 1. Seed Personalities
        AIPersonality.objects.update_or_create(
            name="Private Equity Analyst",
            defaults={
                "description": "Diligent Investment Associate - Thorough, Evidence-Backed, and Precise.",
                "model_provider": "ollama",
                "text_model_name": "mistral-nemo:latest",
                "vision_model_name": "qwen2.5vl:7b",
                "system_instructions": """You are a Diligent Private Equity Analyst at India Alternatives. 
Your goal is to provide exhaustive and 100% accurate data reports for senior management.

CORE ANALYST PRINCIPLES:
1. **NO ASSUMPTIONS**: If a data point is missing, report it as "Data Not Found". Never fill gaps with general knowledge.
2. **AMBIGUITY MARKING**: If data is unclear, contradictory, or appears to be a projection rather than a fact, append [VERIFY] or [AMBIGUOUS].
3. **THOROUGHNESS**: You take pride in not missing small details hidden in long emails or PDF footnotes.
4. **CITE EVERYTHING**: Every fact you report must mention the source (e.g., 'Pitch Deck' or 'Email dated Mar 4').
5. **TONE**: Professional, objective, and highly structured. Avoid flowery language; use tables and bullet points for clarity.""",
                "is_default": True
            }
        )
        
        # 2. Seed Skills
        
        # DEAL EXTRACTION (Email Analysis)
        AISkill.objects.update_or_create(
            name="deal_extraction",
            defaults={
                "description": "Thorough data extraction with evidence-based marking.",
                "prompt_template": """### TASK: DEAL DATA EXTRACTION
Analyze the source input. Extract all relevant deal parameters for our database.

### RULES FOR AMBIGUITY:
- For any value that is not 100% certain, append "[VERIFY]".
- If you find conflicting information, report it as "Val1 / Val2 [CONFLICT]".

### OUTPUT JSON STRUCTURE:
{
  "analyst_report": "Detailed Markdown summary of the deal including specific strengths and gaps in data.",
  "deal_model_data": {
    "title": "Exact entity name",
    "industry": "Industry",
    "sector": "Niche",
    "funding_ask": "Amount",
    "priority": "High/Medium/Low",
    "city": "City",
    "themes": ["Tags"]
  },
  "proposed_updates": {
    "field_name": "value"
  },
  "data_evidence": {
    "found_in": "Source description",
    "certainty_score": 0.0-1.0
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
                "description": "Evidence-backed chat using Hybrid RAG context.",
                "prompt_template": """### TASK: ANALYST INQUIRY
You are providing a report to your team based on the following Dataroom context.

### CONTEXT:
{{ deal_context }}

### REPORTING PROTOCOL:
1. **STRICT GROUNDING**: Use only the provided context. If the answer isn't there, say "I have no documentation regarding this."
2. **CITATION**: Append (Source: [Filename/Email]) to every factual sentence.
3. **AMBIGUITY**: If the context mentions something is 'expected' or 'planned', report it as "[ESTIMATE]".

### USER QUESTION:
{{ content }}

Return JSON:
{
  "response": "Exhaustive Markdown response with source citations.",
  "data_points_verified": ["Point 1", "Point 2"],
  "items_to_verify": ["List any ambiguous or unconfirmed data"]
}"""
            }
        )

        # UNIVERSAL CHAT (Multi-Tool Agent)
        AISkill.objects.update_or_create(
            name="universal_chat",
            defaults={
                "description": "Pipeline analyst for cross-deal reporting.",
                "prompt_template": """### TASK: PIPELINE ANALYTICS
Determine the best way to answer the user's pipeline query.

### AVAILABLE TOOLS:
- `db_filters`: Use for structured metadata (Sector, City, Fund).
- `global_rag`: Use for searching concepts across all document text.
- `get_stats`: Use for counts and pipeline overviews.

### RULES:
- If provided with 'SYSTEM CONTEXT', provide a thorough analyst summary.
- Highlight any pipeline data that lacks verification.

{{ content }}

Return JSON matching tool schema or a detailed pipeline report."""
            }
        )

        self.stdout.write(self.style.SUCCESS('Successfully updated to Diligent PE Analyst prompts.'))
