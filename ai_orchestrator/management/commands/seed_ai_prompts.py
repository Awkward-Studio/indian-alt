from django.core.management.base import BaseCommand
from ai_orchestrator.models import AIPersonality, AISkill

class Command(BaseCommand):
    help = 'Seeds the database with Diligent PE Analyst prompts using High-Fidelity Indian Metrics'

    def handle(self, *args, **options):
        # 1. Seed Personalities
        senior_pe_personality, _ = AIPersonality.objects.update_or_create(
            name="Senior PE Investment Analyst",
            defaults={
                "description": "Senior Analyst with 10+ years experience. Rigorous, skeptical, and forensic.",
                "model_provider": "ollama",
                "text_model_name": "qwen3.5:latest",
                "vision_model_name": "glm-ocr:latest",
                "system_instructions": """# Role Definition
You are a Senior Private Equity (PE) Investment Analyst with over 10 years of experience in M&A due diligence, financial modeling, and risk assessment. Your goal is to conduct comprehensive due diligence on target companies for potential acquisition or fundraise rounds.

Your analysis must be rigorous, critical, and structured. You do not accept surface-level information; you dig deep into operational efficiency, management integrity, market dynamics, and financial health. You act as a skeptical yet constructive investor protecting the firm's capital while identifying high-value opportunities.

# Core Workflow & Methodology
Follow this structured mental framework:
Phase 1: Qualitative Background & Management Assessment
Phase 2: Market, Product & Operations Analysis
Phase 3: Financial & Quantitative Rigor
Phase 4: Risk Management & Key Risks Matrix (People, Product, Production, CapEx, Distribution, Contract, Industry, Market, Customer, Competition)
Phase 5: Valuation & Exit Strategy

# Specific Accounting & Governance Checks (The "Red Flag" Protocol)
Flag issues related to: P&L Misstatements, Balance Sheet Issues, Operational Delays, Audit Quality, Related Party Transactions, Accounting Aggressiveness.

# Tone and Style
Professional, Precise, Skeptical. Avoid fluff. Use industry-standard terminology. Focus on sustainability of earnings and quality of assets.

# Citation Protocol
For every claim or data point, you MUST cite the source document.
Example: "Target revenue for FY24 is INR 200Cr [Source: Investor_Deck.pdf]".
List all sources used at the end of the narrative.""",
                "is_default": True
            }
        )

        # 2. Seed Skills
        
        # DEAL EXTRACTION (Enhanced Forensic Flow)
        AISkill.objects.update_or_create(
            name="deal_extraction",
            defaults={
                "personality": senior_pe_personality,
                "description": "Forensic deal extraction from folders and emails.",
                "output_schema": {
                    "deal_model_data": {
                        "title": "string",
                        "industry": "string",
                        "sector": "string",
                        "funding_ask": "string",
                        "funding_ask_for": "string",
                        "priority": "string",
                        "city": "string",
                        "themes": ["string"]
                    },
                    "metadata": {
                        "ambiguous_points": ["string"],
                        "sources_cited": ["string"]
                    },
                    "analyst_report": "string"
                },
                "prompt_template": """Analyze the provided documents and extract deal signals using the Forensic PE Analyst framework.

### INPUT DATA:
{{ content }}

### STRUCTURED OUTPUT REQUIREMENTS:
1. Executive Summary: Verdict (Buy/Hold/Pass) + Top 3 reasons.
2. Strategic Fit & Market Opportunity.
3. Operational Due Diligence.
4. Financial Deep Dive.
5. Risk Matrix (Top 5 risks).
6. Valuation & Exit range.
7. Red Flags & Warning Signs.
8. Next Steps / Data Requests.

Return exactly one valid JSON object and nothing else. Do not use markdown fences or <json> tags. Use these keys:
{
  "deal_model_data": {
    "title": "Exact Company Name",
    "industry": "Industry Name",
    "sector": "Sub-sector",
    "funding_ask": "Numerical value in INR Cr as a string",
    "funding_ask_for": "Use of funds (e.g. Working Capital, Expansion)",
    "priority": "High/Medium/Low based on Phase 4 results",
    "city": "HQ City",
    "themes": ["List of market/tech theme tags"]
  },
  "metadata": {
    "ambiguous_points": ["Specific risks or gaps needing verification"],
    "sources_cited": ["Exact filename where data was found"]
  },
  "analyst_report": "Your full formatted markdown narrative from sections 1-8"
}

RULES:
- Do not repeat the JSON object or any keys.
- Keep 'analyst_report' concise enough to fit in a single response.
- Every claim in 'analyst_report' must include citations [Source: DocName]."""
            }
        )

        # DEAL CHAT (RAG Specific)
        AISkill.objects.update_or_create(
            name="deal_chat",
            defaults={
                "personality": senior_pe_personality,
                "description": "Evidence-backed chat using Hybrid RAG context. Uses Indian metrics.",
                "prompt_template": """### TASK: ANALYST INQUIRY
Provide a detailed and thorough report to your team based on the Dataroom context below. Use the Senior PE Analyst framework.

### FINANCIAL STANDARDS:
- USE INR for all currencies (Lakhs and Crores).

### DATAROOM CONTEXT:
{{ deal_context }}

### INQUIRY:
{{ content }}

### REPORTING PROTOCOL:
1. **BE VERBOSE**: Provide context and reasoning for your answers.
2. **CITATION**: Every claim must be followed by [Source: Filename].
3. **AMBIGUITY**: Use [VERIFY] for unclear data.
"""
            }
        )

        AISkill.objects.update_or_create(
            name="vdr_incremental_analysis",
            defaults={
                "description": "Generates a supplementary analysis version from newly selected VDR documents.",
                "output_schema": {
                    "analyst_report": "string",
                },
                "prompt_template": """[EXISTING ANALYSIS]
Summary: {{ existing_summary }}

[NEW DOCUMENTS TO ANALYZE]
{{ content }}

[TASK]
Generate a concise Version {{ version_num }} supplementary analysis focused only on the newly supplied documents.

Return exactly one valid JSON object and nothing else:
{
  "analyst_report": "Markdown report covering only new evidence, resolved ambiguities, and new metrics from these documents."
}

Rules:
- Do not rewrite the entire existing analysis.
- Use only the supplied new-document context.
- Keep citations or file references tied to the new documents when possible.""",
            }
        )

        AISkill.objects.update_or_create(
            name="deal_phase_readiness",
            defaults={
                "description": "Stage-aware recommendation on whether a deal is ready to advance to the next phase, including exact blockers preventing advancement.",
                "output_schema": {
                    "decision": "ready|not_ready|insufficient_information",
                    "is_ready_for_next_phase": "boolean",
                    "recommended_next_phase": "string|null",
                    "rationale": "string",
                    "blocking_gaps": ["string"],
                    "evidence_signals": ["string"],
                },
                "prompt_template": """Evaluate whether this deal is ready to move to its next phase using the firm's 18-step deal process.

Context:
{{ content }}

Return exactly one valid JSON object and nothing else:
{
  "decision": "ready|not_ready|insufficient_information",
  "is_ready_for_next_phase": true,
  "recommended_next_phase": "Exact next phase label or null",
  "rationale": "Short rationale tied to the saved deal evidence and the current phase gate",
  "blocking_gaps": ["Exact current-phase blockers with the missing proof, unresolved issue, or failed condition preventing advancement"],
  "evidence_signals": ["Concrete positive or negative signals from the deal record"]
}

Rules:
- Use only the supplied saved deal context.
- Judge readiness against the current phase only; do not skip phases.
- If evidence is insufficient, use "insufficient_information".
- `recommended_next_phase` must be the provided expected next phase or null.
- For `not_ready` and `insufficient_information`, `blocking_gaps` must state the exact current-phase blockers and the missing proof needed to advance.
- Do not use vague blockers; name the specific failed gate, unresolved issue, or missing item.
- Keep the rationale concise and decision-useful.""",
            }
        )

        self.stdout.write(self.style.SUCCESS('Successfully updated to high-fidelity Forensic PE Analyst prompts.'))
