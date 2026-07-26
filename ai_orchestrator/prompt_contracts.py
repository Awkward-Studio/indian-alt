"""Shared AI prompt contracts for app skills and offline bulk pipelines."""

PHASE2_ARTIFACT_REQUIRED_KEYS = (
    "document_type_suggestion",
    "document_summary",
    "claims",
    "metrics",
    "numeric_evidence",
    "table_definitions",
    "risks",
    "open_questions",
    "diligence_gaps",
    "citations",
)

PHASE2_ARTIFACT_KEYS = PHASE2_ARTIFACT_REQUIRED_KEYS + (
    "document_name",
    "document_type",
    "tables_summary",
    "contacts_found",
    "quality_flags",
    "normalized_text",
    "source_map",
    "source_metadata",
    "spreadsheet_profile",
    "reasoning",
)

IC_REPORT_HEADERS = (
    "## Company Details",
    "## Promoter and Management Details",
    "## Industry Overview",
    "## Transaction Details",
    "## Key Financials",
    "## Transaction / Trading Multiples",
    "## Risk Factors",
    "## Investment Rationale",
    "## Exit Considerations",
    "## Next Steps",
)

IC_SECTION_TITLES = tuple(header.replace("## ", "") for header in IC_REPORT_HEADERS)

CONTACT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": ["string", "null"]},
        "email": {"type": ["string", "null"]},
        "designation": {"type": ["string", "null"]},
        "linkedin_url": {"type": ["string", "null"]},
        "phone": {"type": ["string", "null"]},
        "location": {"type": ["string", "null"]},
        "bank_name": {"type": ["string", "null"]},
        "bank_domain": {"type": ["string", "null"]},
    },
    "required": [
        "name",
        "email",
        "designation",
        "linkedin_url",
        "phone",
        "location",
        "bank_name",
        "bank_domain",
    ],
    "additionalProperties": False,
}

BANK_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": ["string", "null"]},
        "website_domain": {"type": ["string", "null"]},
        "description": {"type": ["string", "null"]},
    },
    "required": ["name", "website_domain", "description"],
    "additionalProperties": False,
}

SOURCE_RELATIONSHIPS_SCHEMA = {
    "type": "object",
    "properties": {
        "bank": BANK_SCHEMA,
        "primary_contact": {"anyOf": [CONTACT_SCHEMA, {"type": "null"}]},
        "additional_contacts": {"type": "array", "items": CONTACT_SCHEMA},
        "relationship_metadata": {
            "type": "object",
            "properties": {
                "source_type": {"type": ["string", "null"]},
                "source_documents": {"type": "array", "items": {"type": "string"}},
                "confidence": {"type": ["string", "null"], "enum": ["High", "Medium", "Low", None]},
                "ambiguities": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["source_type", "source_documents", "confidence", "ambiguities"],
            "additionalProperties": False,
        },
    },
    "required": ["bank", "primary_contact", "additional_contacts", "relationship_metadata"],
    "additionalProperties": False,
}

DEAL_SYNTHESIS_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "deal_model_data": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "industry": {"type": "string"},
                "sector": {"type": "string"},
                "funding_ask": {"type": "string"},
                "funding_ask_for": {"type": "string"},
                "priority": {"type": "string", "enum": ["High", "Medium", "Low"]},
                "city": {"type": "string"},
                "state": {"type": "string"},
                "country": {"type": "string"},
                "themes": {"type": "array", "items": {"type": "string"}},
                "is_female_led": {"type": "boolean"},
                "deal_summary": {"type": "string"},
                "deal_details": {"type": "string"},
                "company_details": {"type": "string"},
                "priority_rationale": {"type": "string"},
            },
            "required": [
                "title",
                "industry",
                "sector",
                "funding_ask",
                "funding_ask_for",
                "priority",
                "city",
                "state",
                "country",
                "themes",
                "is_female_led",
                "deal_summary",
                "deal_details",
                "company_details",
                "priority_rationale",
            ],
            "additionalProperties": False,
        },
        "source_relationships": SOURCE_RELATIONSHIPS_SCHEMA,
        "analyst_report": {"type": "string"},
        "metadata": {
            "type": "object",
            "properties": {
                "ambiguous_points": {"type": "array", "items": {"type": "string"}},
                "documents_analyzed": {"type": "array", "items": {"type": "string"}},
                "cross_document_conflicts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "topic": {"type": "string"},
                            "details": {"type": "string"},
                            "citations": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["topic", "details", "citations"],
                        "additionalProperties": False,
                    },
                },
                "missing_information_requests": {"type": "array", "items": {"type": "string"}},
            },
            "required": [
                "ambiguous_points",
                "documents_analyzed",
                "cross_document_conflicts",
                "missing_information_requests",
            ],
            "additionalProperties": False,
        },
    },
    "required": ["deal_model_data", "source_relationships", "analyst_report", "metadata"],
    "additionalProperties": False,
}

REPORT_FORMAT_REQUIREMENTS = """
ANALYST_REPORT REQUIREMENTS:
- The `analyst_report` field must be well-formatted markdown, not plain paragraphs.
- Write like an institutional PE IC note suitable for internal circulation.
- Use clear section headings with `##` markdown headers.
- Use bullets and compact markdown tables where they improve readability.
- If a section has limited evidence, say that explicitly rather than fabricating detail.
- Do not include any external facts. External/public/Claude/web/Venture Intelligence/Screener items must be phrased as diligence asks only.
- Mention source documents in each section where possible.
- In each major section, include a compact `Next steps / further diligence / red flags` table unless the section itself is the final Next Steps table.
- Every report must follow this exact section order:
  1. `## Company Details`
     - About the company, products/services, core focus, major revenue source, key investor concerns, existing investors, source documents.
  2. `## Promoter and Management Details`
     - Promoter/founder background, designation, prior experience, education if available, why they are suited for the business, and unusual items/red flags visible in internal materials.
  3. `## Industry Overview`
     - Demand, market size/TAM if internally available, competition, peer positioning, moat, supply chain, supply constraints, regulation/pricing/logistics/contracts. Mark missing public market validation as External diligence required.
  4. `## Transaction Details`
     - Fund raise ask, proposed IA investment amount, instrument, pre-money valuation, IA ownership, revenue/EBITDA valuation multiples, source of deal, lead investor, existing investor follow-on, total funds raised till date.
  5. `## Key Financials`
     - Condensed historical and projected P&L where available: gross revenue, segment/channel revenue, net revenue, gross margin, segment/channel GM, CM1/CM2/CM3, corporate expenses, EBITDA. Include balance sheet, receivables/days, payables/days on sales, inventory/days, NWC, ROCE/ROIC/ROE formula and result where internally supported.
  6. `## Transaction / Trading Multiples`
     - Transaction multiple table with Company, Acquirer / Investor, Deal Date, Deal Size, Company Valuation - PreMoney, Revenue Multiple, EBITDA Multiple. Trading comparable table with Market Cap, 2 Year Revenue CAGR, Price/Revenue, Total Revenues, EBITDA Margin, PAT Margin, Debt, Cash and cash equivalents. If missing internally, mark External diligence required.
  7. `## Risk Factors`
     - Table with columns: Key Risk, Probability, Mitigants and IA comments. Cover qualitative and quantitative risks supported by internal evidence.
  8. `## Investment Rationale`
     - 5-10 factual, hard-hitting rationales only if supported by evidence. Do not force positives; negatives belong in Risk Factors.
  9. `## Exit Considerations`
     - Use available entry valuation, implied multiples, IA stake, dilution assumptions and exit assumptions. Leave blanks / evidence unavailable where not supported.
  10. `## Next Steps`
      - Table with columns: Serial Number, Tasks / Next Step, Task Owner, Task assigned to, Status.
- Do not wrap the markdown in code fences.
- Keep the report crisp, decision-oriented, evidence-led, and unbiased.
"""

PHASE3_SYSTEM_PROMPT = """You are a Senior Investment Analyst at India Alternatives preparing an internal PE investor-grade IC note.
If a requested item requires external validation, write "External diligence required" and add a concrete diligence ask. Do not invent the answer.
Internal materials are company-provided and may be promotional. Cut through marketing language, stay unbiased, identify risks, validate repeated numbers across documents, and produce a decision-useful IC note.
Every factual statement must be traceable to the provided internal documents. Prefer Pitch Deck / IM for company, products, promoter, transaction overview; Cap Table for investors and shareholding; Financial Model for P&L, projections, margins, balance sheet, working capital and return ratios; transaction docs for round details; internal market reports / DRHP / brokerage materials only if present in the input for industry analysis.
Also extract the source bank/advisory firm and banker relationships for later import into the app.

RELATIONSHIP RULES:
- Populate source_relationships.bank with the bank or advisory firm the deal came from.
- Populate source_relationships.primary_contact with the main banker/contact when identifiable.
- Populate source_relationships.additional_contacts only with clearly identified secondary bankers/advisors.
- If only a bank is known, keep bank populated and primary_contact null.
- If a contact is known but the bank is unclear, keep the contact, leave bank fields null if needed, and explain the gap in relationship_metadata.ambiguities.
- Use source_documents to list the filenames that support the relationship extraction.
- Keep deal attributes in deal_model_data and relationship data in source_relationships. Do not mix them.
- Use both structured Phase 2 evidence and normalized-text evidence summaries provided per document."""

DEAL_SYNTHESIS_SYSTEM_TEMPLATE = f"""### INTERNAL IC DEAL SYNTHESIS SKILL
Use this skill for canonical deal synthesis and deal-helper full rewrites.
Follow the output mode in the user prompt:
- `canonical_json` or blank: return the structured deal synthesis JSON required by deal creation.
- `markdown_document`: return only the final internal IC Markdown document.

{PHASE3_SYSTEM_PROMPT}

Evidence discipline always applies:
- Treat document_evidence_json as the primary source of truth.
- Use supporting raw chunks, selected deal helper context, stored related-deal context, selected pipeline context, and deal-specific directives only to refine the analysis.
- Do not invent values absent from evidence; use N/A, [VERIFY], or External diligence required.
- Preserve citations/source names for material factual claims."""

DEAL_SYNTHESIS_PROMPT_TEMPLATE = f"""Synthesize deal analysis from structured Phase 2 document evidence, supporting raw chunks, selected analyst context, saved deal-specific directives, stored competitor context, and selected pipeline context.

[OUTPUT MODE]
{{{{ output_mode }}}}

[DEAL BASELINE JSON]
{{{{ deal_baseline_json }}}}

[DOCUMENT EVIDENCE JSON]
{{{{ document_evidence_json }}}}

[SUPPORTING RAW CHUNKS JSON]
{{{{ supporting_raw_chunks_json }}}}

[DEAL-SPECIFIC ANALYSIS DIRECTIVE]
{{{{ deal_specific_prompt }}}}

[STORED COMPETITOR / RELATED-DEAL CONTEXT]
{{{{ related_deal_context }}}}

[SELECTED PIPELINE CONTEXT]
{{{{ selected_pipeline_context }}}}

[SELECTED DEAL HELPER CONTEXT]
{{{{ selected_context }}}}

[TASK]
{{{{ content }}}}

{REPORT_FORMAT_REQUIREMENTS}

If OUTPUT MODE is `markdown_document`, return only a Markdown document following the 10-section internal IC note structure above. Do not return JSON, metadata, document_evidence, markdown fences, or prompt instructions.

If OUTPUT MODE is blank or `canonical_json`, return exactly one valid JSON object and nothing else with this top-level shape:
{{
  "deal_model_data": {{
    "title": "Exact Company Name",
    "industry": "Industry Name",
    "sector": "Sub-sector",
    "funding_ask": "Numerical value in INR Cr as a string",
    "funding_ask_for": "Use of funds",
    "priority": "High/Medium/Low",
    "city": "HQ City",
    "state": "HQ State",
    "country": "Country",
    "themes": ["India Alternatives themes only"],
    "is_female_led": false,
    "deal_summary": "Short deal summary",
    "deal_details": "Transaction and business details",
    "company_details": "Company facts",
    "priority_rationale": "Why this priority is justified"
  }},
  "source_relationships": {{
    "bank": {{"name": null, "website_domain": null, "description": null}},
    "primary_contact": null,
    "additional_contacts": [],
    "relationship_metadata": {{"source_type": null, "source_documents": [], "confidence": null, "ambiguities": []}}
  }},
  "metadata": {{
    "ambiguous_points": [],
    "sources_cited": [],
    "documents_analyzed": [],
    "analysis_input_files": [],
    "failed_files": [],
    "cross_document_conflicts": [],
    "missing_information_requests": []
  }},
  "analyst_report": "Markdown internal IC memo using the required 10-section structure, with citations/source names",
  "document_evidence": [],
  "cross_document_conflicts": [],
  "missing_information_requests": []
}}

Rules:
- Apply deal_specific_prompt as an additional deal-level writing directive; it augments this skill prompt and must not replace the evidence discipline or output mode.
- Use related_deal_context for competitor, peer, comparable, parent, subsidiary, customer, or vendor context when present.
- If unrelated documents are present, explicitly flag them as excluded or low relevance.
- Do not invent values absent from evidence; use N/A, [VERIFY], or External diligence required and add missing-information requests where the output mode supports them."""

DOCUMENT_EVIDENCE_SYSTEM_TEMPLATE = """### PHASE 2 DOCUMENT EVIDENCE OUTPUT CONTRACT
- Return exactly one JSON object and nothing else.
- Use the Phase 2 artifact field names expected by downstream Phase 3 synthesis.
- `claims`, `risks`, `open_questions`, `diligence_gaps`, `citations`, and `quality_flags` must be arrays of strings.
- `metrics`, `numeric_evidence`, `table_definitions`, `tables_summary`, and `contacts_found` must be arrays.
- `normalized_text` must preserve material detail for downstream chunking."""

DOCUMENT_EVIDENCE_PROMPT_TEMPLATE = """Analyze this single internal deal document and return a Phase 2 structured evidence artifact for downstream internal IC synthesis.

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
  "document_type_suggestion": {
    "label": "Pitch Deck / IM / Financial Model / Teaser / Cap Table / Transaction Document / Email / Other",
    "display_label": "Human readable document type",
    "confidence": "High/Medium/Low",
    "rationale": "Why this type was selected"
  },
  "document_summary": "2-4 sentence summary of material points",
  "claims": ["Important factual statements supported by this document"],
  "metrics": [
    {
      "name": "Metric name",
      "value": "Metric value as string",
      "period": "Applicable period if known",
      "unit": "INR Cr / % / x / etc",
      "source_location": "Document/page/sheet/range reference",
      "confidence": "High/Medium/Low"
    }
  ],
  "numeric_evidence": [
    {
      "line_item": "Financial or operating line item",
      "value": "Value as string",
      "period": "Period",
      "unit": "INR Cr / % / x / etc",
      "source_location": "Document/page/sheet/range reference",
      "confidence": "High/Medium/Low",
      "notes": "Context or caveat"
    }
  ],
  "table_definitions": [
    {
      "title": "Table label",
      "sheet_name": "Sheet name if applicable",
      "range": "Cell/range/page reference if applicable",
      "detected_header_rows": [],
      "period_columns": [],
      "metric_rows": [],
      "units": "Units",
      "key_highlights": [],
      "source_location": "Document/page/sheet/range reference"
    }
  ],
  "tables_summary": [],
  "contacts_found": [],
  "risks": ["Material risks or red flags in this document"],
  "open_questions": ["Questions created by missing or unclear information"],
  "diligence_gaps": ["Concrete diligence asks implied by this document"],
  "citations": ["Document/page/sheet/range references used"],
  "quality_flags": [],
  "normalized_text": "Cleaned normalized text preserving material details, numbers, tables, contacts, and citations",
  "source_map": {"document_name": "{{ document_name }}", "section": null, "page": null},
  "source_metadata": {},
  "spreadsheet_profile": {},
  "reasoning": "Short explanation of what mattered in this document"
}

Rules:
- Use only the supplied document.
- Preserve all material numbers, table/range definitions, risks, claims, and diligence gaps.
- Cite sheet/range/row/page/part locations when available.
- Do not add web, public-market, or general industry facts."""

