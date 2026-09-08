"""Shared prompt text mirrored from the bulk_2 and bulk_3 pipelines."""

from __future__ import annotations

import json


BULK2_INTEL_SYSTEM_PROMPT = """[SYSTEM: INTERNAL-DOCUMENT-EVIDENCE-EXTRACTION]
Use only the supplied internal document content and metadata. Do not use public knowledge, web knowledge, or assumptions.
Return JSON only. Extract investor-grade evidence for a PE analyst preparing an IC note.
Be precise with numbers and preserve source locations such as sheet/range/row references."""


def build_bulk2_segment_prompt(*, segment: str, context: dict, compact: bool = False) -> str:
    compact_instruction = (
        "COMPACT MODE: The segment is short or a previous response was too long. "
        "Return the smallest valid JSON. Use at most 3 claims, 3 metrics, 3 numeric_evidence items, "
        "1 table_definitions item, 3 risks, 3 open_questions, 3 diligence_gaps, and 3 citations. "
        "Keep every string short. Empty arrays are acceptable when evidence is not present.\n"
        if compact
        else ""
    )
    return (
        "Extract structured internal-document evidence from this segment for PE IC-note synthesis.\n"
        "Use only this supplied segment and metadata. Do not add web/public facts.\n"
        "Preserve material numbers, table/range definitions, risks, claims, and diligence gaps visible in this segment.\n"
        "Cite sheet/range/row/part locations using the provided source_location.\n"
        "Be concise; do not repeat the same fact in multiple fields.\n"
        f"{compact_instruction}\n"
        f"[SEGMENT CONTEXT JSON]\n{json.dumps(context, default=str)}\n\n"
        f"[CLEANED INTERNAL DOCUMENT SEGMENT]\n{segment}"
    )


BULK3_DOCUMENT_SUMMARY_SYSTEM_PROMPT = (
    "Return concise markdown bullets only. No JSON. No external information."
)
BULK3_DOCUMENT_SUMMARY_PROMPT = (
    "You are summarizing internal deal material for a PE IC note. Use only the text provided. "
    "Extract investor-grade evidence, not marketing fluff. Include source document name, financial "
    "numbers, periods, units, risks, diligence gaps, and table definitions if visible. If nothing "
    "useful is present, say evidence unavailable. Do not use external knowledge."
)


BULK3_SECTION_INSTRUCTIONS = {
    "Executive Summary": "Give a preliminary verdict, the strongest supporting facts, principal risks, and immediate diligence priorities using only internal evidence.",
    "Company Details": "Write about company, products/services, core focus, revenue sources, investors, key highlights and concerns.",
    "Promoter and Management Details": "Write promoter/management background, experience, designation, education if available, suitability, and internal-material red flags.",
    "Industry Overview": "Write industry demand, TAM/market size if internally available, competition, moat, value chain, supply constraints and diligence asks.",
    "Transaction Details": "Write fund raise ask, IA investment, instrument, valuation, ownership, round leader, follow-on, total funds raised and sourcing.",
    "Key Financials": "Write condensed P&L, revenue breakdown, margins, contribution margins, expenses, EBITDA, working capital, balance sheet and return ratios where supported.",
    "Transaction / Trading Multiples": "Write transaction/trading comparable tables only from internal evidence; mark missing external validation as diligence required.",
    "Risk Factors": "Write a table with Key Risk, Probability, Mitigants and IA comments; include qualitative and quantitative risks.",
    "Investment Rationale": "Write 5-10 factual hard-hitting rationales supported by evidence; do not force positives.",
    "Exit Considerations": "Write entry valuation, implied multiples, dilution assumptions and exit assumptions where internally supported.",
    "Next Steps": "Write a diligence task table with Serial Number, Tasks / Next Step, Task Owner, Task assigned to, Status.",
}

