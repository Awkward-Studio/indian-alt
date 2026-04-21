from django.core.management.base import BaseCommand
from django.conf import settings
from ai_orchestrator.models import AIPersonality, AISkill

class Command(BaseCommand):
    help = 'Seeds the database with Diligent PE Analyst prompts using High-Fidelity Indian Metrics'

    def handle(self, *args, **options):
        # 1. Seed Personalities
        senior_pe_personality, _ = AIPersonality.objects.update_or_create(
            name="Senior PE Investment Analyst",
            defaults={
                "description": "Senior Analyst with 10+ years experience. Rigorous, skeptical, and forensic.",
                "model_provider": "vllm",
                "text_model_name": getattr(settings, "VLLM_TEXT_MODEL", "") or "default",
                "vision_model_name": getattr(settings, "VLLM_VISION_MODEL", "") or "default",
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
                    "contact_discovery": {
                        "firm_name": "string",
                        "firm_domain": "string",
                        "name": "string",
                        "designation": "string",
                        "linkedin": "string",
                        "email": "string"
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
    "themes": ["List of India Alternative themes"]
  },
  "contact_discovery": {
    "firm_name": "Investment Bank or Advisory Firm Name",
    "firm_domain": "Website domain of the firm (e.g. website.com)",
    "name": "Primary Banker or Contact Name",
    "designation": "Designation/Title of the Contact",
    "linkedin": "LinkedIn URL if available",
    "email": "Email address of the contact if available"
  },
  "metadata": {
    "ambiguous_points": ["Specific risks or gaps needing verification"],
    "sources_cited": ["Exact filename where data was found"]
  },
  "analyst_report": "Your full formatted markdown narrative from sections 1-8"
}

RULES:
- THEMES: Every deal MUST be linked to one or more of the following India Alternative themes ONLY. Do not use any other themes:
  a. Women Oriented Consumption
  b. Health & Wellness
  c. Financial Services + Tech
  d. Gen Z + Millennials
  e. Climate & Sustainability
- BANKERS: Investment Banker and advisory team details are typically located on the final pages of the pitch deck or teaser. Extract them carefully into the 'contact_discovery' object. If none are found, output null for contact_discovery.
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
            name="document_evidence_extraction",
            defaults={
                "personality": senior_pe_personality,
                "description": "Extracts structured evidence from a single document before final deal synthesis.",
                "output_schema": {
                    "document_name": "string",
                    "document_type": "string",
                    "document_summary": "string",
                    "claims": ["string"],
                    "metrics": ["object"],
                    "tables_summary": ["object"],
                    "contacts_found": ["object"],
                    "risks": ["string"],
                    "open_questions": ["string"],
                    "citations": ["string"],
                    "reasoning": "string",
                    "quality_flags": ["string"],
                    "normalized_text": "string",
                    "source_map": "object",
                },
                "prompt_template": """Analyze this single document and return a structured evidence object for downstream deal synthesis.

[DOCUMENT NAME]
{{ document_name }}

[DOCUMENT TYPE]
{{ document_type }}

[SOURCE METADATA]
{{ source_metadata_json }}

[DOCUMENT CONTENT]
{{ content }}

Return exactly one valid JSON object and nothing else:
{
  "document_name": "{{ document_name }}",
  "document_type": "{{ document_type }}",
  "document_summary": "2-4 sentence summary of the document's most material points",
  "claims": ["Important factual statements supported by this document"],
  "metrics": [
    {
      "name": "Metric name",
      "value": "Metric value as string",
      "period": "Applicable period if known",
      "unit": "INR Cr / % / x / etc",
      "citation": "Document/page reference"
    }
  ],
  "tables_summary": [
    {
      "title": "Short table label",
      "highlights": ["Most important rows or insights"],
      "citation": "Document/page reference"
    }
  ],
  "contacts_found": [
    {
      "name": "Person name",
      "designation": "Role/title",
      "email": "Email if present",
      "citation": "Document/page reference"
    }
  ],
  "risks": ["Material risks or red flags in this document"],
  "open_questions": ["Questions created by missing or unclear information in this document"],
  "citations": ["Document/page references used"],
  "reasoning": "Short internal reasoning trace about what mattered in this document",
  "quality_flags": ["Use flags like fallback_artifact, low_confidence, limited_tables when needed"],
  "normalized_text": "A cleaned normalized version of the document, concise but preserving material detail",
  "source_map": {
    "document_name": "{{ document_name }}",
    "section": null,
    "page": null
  }
}

Rules:
- Use only the supplied document.
- Keep normalized_text concise enough for downstream chunking.
- Every metric or contact should include a citation when possible.
- Do not infer missing facts unless you label them as open questions.""",
            }
        )

        AISkill.objects.update_or_create(
            name="deal_synthesis",
            defaults={
                "personality": senior_pe_personality,
                "description": "Synthesizes a final deal analysis from document evidence objects and supporting raw chunks.",
                "output_schema": {
                    "deal_model_data": "object",
                    "metadata": "object",
                    "analyst_report": "string",
                    "document_evidence": "array",
                    "cross_document_conflicts": "array",
                    "missing_information_requests": "array",
                },
                "prompt_template": """Synthesize a final deal analysis from structured document evidence plus supporting raw chunks.

[DOCUMENT EVIDENCE JSON]
{{ document_evidence_json }}

[SUPPORTING RAW CHUNKS JSON]
{{ supporting_raw_chunks_json }}

[TASK]
{{ content }}

Return exactly one valid JSON object and nothing else:
{
  "deal_model_data": {
    "title": "Exact Company Name",
    "industry": "Industry Name",
    "sector": "Sub-sector",
    "funding_ask": "Numerical value in INR Cr as a string",
    "funding_ask_for": "Use of funds",
    "priority": "High/Medium/Low",
    "city": "HQ City",
    "themes": ["India Alternative themes only"]
  },
  "metadata": {
    "ambiguous_points": ["Unresolved points requiring verification"],
    "sources_cited": ["Document names or citation labels used"],
    "documents_analyzed": ["Document names included in this synthesis"],
    "analysis_input_files": [{"file_id": "source id", "file_name": "Document name"}],
    "failed_files": [{"file_id": "source id", "file_name": "Document name", "reason": "Why it failed"}]
  },
  "analyst_report": "Final markdown memo with citations",
  "document_evidence": [],
  "cross_document_conflicts": [
    {
      "topic": "Metric or claim in conflict",
      "details": "Why the documents conflict",
      "citations": ["Source labels"]
    }
  ],
  "missing_information_requests": ["Follow-up diligence requests implied by the evidence"]
}

Rules:
- Treat document_evidence_json as the primary source of truth.
- Use supporting_raw_chunks_json only to refine detail and citations.
- Preserve citations in every material section of analyst_report.
- Surface contradictions explicitly in cross_document_conflicts.
- Include metadata.documents_analyzed and metadata.analysis_input_files in the final JSON.
- Do not invent values absent from the evidence.""",
            }
        )

        AISkill.objects.update_or_create(
            name="vdr_incremental_analysis",
            defaults={
                "description": "Generates a supplementary analysis version from newly selected VDR documents.",
                "output_schema": {
                    "analyst_report": "string",
                    "deal_model_data": "object",
                    "metadata": "object",
                    "document_evidence": "array",
                    "cross_document_conflicts": "array",
                    "missing_information_requests": "array",
                },
                "prompt_template": """[EXISTING ANALYSIS]
Summary: {{ existing_summary }}

[CURRENT CANONICAL SNAPSHOT]
{{ existing_canonical_snapshot }}

[NEW DOCUMENT EVIDENCE JSON]
{{ document_evidence_json }}

[SUPPORTING RAW CHUNKS JSON]
{{ supporting_raw_chunks_json }}

[TASK]
Generate a concise Version {{ version_num }} supplementary analysis focused only on the newly supplied documents, and provide structured field updates for the canonical deal view when supported by the new evidence.

Return exactly one valid JSON object and nothing else:
{
  "analyst_report": "Markdown report covering only new evidence, resolved ambiguities, and new metrics from these documents.",
  "deal_model_data": {},
  "metadata": {
    "ambiguous_points": [],
    "documents_analyzed": ["New document names included in this version"],
    "analysis_input_files": [{"file_id": "source id", "file_name": "Document name"}],
    "failed_files": []
  },
  "document_evidence": [],
  "cross_document_conflicts": [],
  "missing_information_requests": []
}

Rules:
- Do not rewrite the entire existing analysis.
- Use the structured document evidence as the primary source of truth.
- Use supporting raw chunks only for more precise citations or nuance.
- Include metadata.documents_analyzed and metadata.analysis_input_files in the final JSON.
- Keep citations or file references tied to the new documents when possible.""",
            }
        )

        self.stdout.write(self.style.SUCCESS('Successfully updated to high-fidelity Forensic PE Analyst prompts.'))
