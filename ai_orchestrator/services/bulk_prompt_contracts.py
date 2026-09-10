"""Shared prompt text mirrored from the bulk_2 and bulk_3 pipelines."""

from __future__ import annotations

import json


BULK2_INTEL_SYSTEM_PROMPT = """[SYSTEM: INTERNAL-DOCUMENT-EVIDENCE-EXTRACTION]
Use only the supplied internal document content and metadata. Do not use public knowledge, web knowledge, or assumptions.
Return JSON only. Extract investor-grade evidence for a PE analyst preparing an IC note.
Read the complete segment before writing. Coverage matters more than brevity.
Copy numbers and factual values exactly. Preserve currency, units, period, actual/budget/forecast status, standalone/consolidated basis, and source locations such as page, section, sheet, range, and row.
Do not turn management claims into verified facts. Record contradictions, unclear labels, missing periods, and broken reconciliations instead of resolving them by assumption."""


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
        "Build a complete structured evidence artifact from this internal-document segment for downstream PE underwriting.\n"
        "Use only the supplied segment and metadata. Do not add public facts, general knowledge, estimates, or inferred values.\n\n"
        "Coverage requirements:\n"
        "1. Financials: capture every disclosed period and material line item across P&L, balance sheet, cash flow, MIS, budget, forecast, unit economics, working capital, debt, cash, capex, valuation, and transaction schedules. Preserve labels, signs, currency, scale, units, period, and actual/budget/forecast status exactly.\n"
        "2. Tables: reconstruct table_definitions with title, headers, period columns, metric rows, units, sheet/range/page location, and key observations. Do not reduce a multi-period table to one headline number.\n"
        "3. Business evidence: capture products, pricing, business model, customers, concentration, channels, geographies, suppliers, facilities, capacity, technology, regulation, market claims, and operating KPIs when stated.\n"
        "4. People and transaction evidence: capture founders, management, ownership, cap table, funding history, proposed security, cheque size, valuation, dilution, use of funds, rights, and conditions when stated.\n"
        "5. Underwriting issues: record risks, inconsistencies, unsupported management claims, open questions, and concrete diligence asks. Do not manufacture a risk unless the segment contains a fact that supports it.\n"
        "6. Industry evidence: keep market size, growth, share, competitor, and regulatory claims tied to an internal citation and the stated period, geography, and methodology.\n\n"
        "Output requirements:\n"
        "- Return one JSON object using exactly these top-level keys: document_name, document_type, document_type_suggestion, document_summary, claims, metrics, numeric_evidence, table_definitions, tables_summary, contacts_found, risks, open_questions, diligence_gaps, citations, industry_overview, reasoning, quality_flags, normalized_text, source_map.\n"
        "- Keep document_summary to four to eight precise sentences about this segment.\n"
        "- Put each distinct reported KPI in metrics and each material financial line item in numeric_evidence. A fact may appear in both only when the KPI and statement-line-item uses are both needed downstream.\n"
        "- Every claim, metric, numeric item, table, risk, and industry finding must carry or name the most specific available source location. Use the supplied source_location when the segment has no finer page, sheet, range, or row marker.\n"
        "- Use confidence High only for directly stated, clearly labelled evidence. Use Medium or Low for ambiguous extraction and explain the ambiguity in notes or quality_flags.\n"
        "- Keep normalized_text empty for segment responses. The pipeline retains the complete source text separately.\n"
        "- Before returning, check silently that no disclosed financial period, table, transaction term, material risk, or named entity in the segment was skipped.\n"
        "- Do not repeat the same fact under several aliases and do not pad empty fields with generic prose.\n"
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
    "Executive Summary": """
Write a self-contained IC opening that lets an investment committee understand the opportunity and the unresolved decision in one section. Cover:
- the company, business model, customer problem, products, geography and current operating stage;
- the proposed transaction, security, round size, valuation, stake and use of funds where disclosed;
- a compact historical and forecast scorecard with periods, units, revenue, growth, gross or contribution margin, EBITDA, cash burn and other decision-critical KPIs;
- the strongest evidence-backed reasons to invest and the strongest counterarguments;
- a preliminary recommendation such as proceed, proceed subject to conditions, hold, or decline, with the conditions that would change it;
- the five to ten most material risks, contradictions, missing facts and immediate diligence gates.
Distinguish reported fact, management forecast and analyst calculation. Do not hide weak evidence behind a confident summary.
""".strip(),
    "Company Details": """
Build a full operating profile. Explain legal identity, incorporation and locations when disclosed; company history and milestones; products and services; customer segments; pricing; sales and distribution channels; geographies; revenue model; delivery model; suppliers and partners; facilities and capacity; intellectual property; technology; regulatory dependencies; and group entities. Add tables for product or segment mix, customers, channels, locations, investors and major milestones where the evidence supports them. Explain the revenue engine and unit economics, not just what the company sells. Reconcile conflicting descriptions or dates across documents. End with operating strengths, concerns and missing company-level diligence.
""".strip(),
    "Promoter and Management Details": """
Assess the promoters, founders, directors and senior management as an underwriting issue. Provide a table with name, current role, tenure, education, prior employers or ventures, functional remit, disclosed ownership and source-backed achievements. Evaluate whether the team covers sales, operations, finance, product, technology and governance needs for the stated plan. Discuss founder dependence, succession, hiring gaps, attrition, related-party exposure, board composition, reporting quality, incentive alignment and any internal inconsistencies or red flags. Separate documentary facts from management claims. List background checks, references and governance items still required.
""".strip(),
    "Industry Overview": """
Define the market narrowly enough to match the company's actual revenue pool. Explain the value chain, customer buying process, demand drivers, adoption barriers, seasonality, supply constraints, regulation and structural risks. Present TAM, SAM, SOM, growth rates and segment sizes only when the internal evidence supplies a period, geography, unit and methodology. Map named competitors in a table with positioning, product, price, scale and differentiation where known. Test each claimed moat against evidence such as cost, distribution, switching costs, data, brand, regulation or capacity. Discuss market concentration and bargaining power. Close with unsupported market claims and the external work needed to validate them.
""".strip(),
    "Transaction Details": """
Reconstruct the proposed transaction and show the math. Cover total raise, IA cheque, other investors, primary versus secondary proceeds, instrument, price per share, pre-money and post-money valuation, fully diluted shares, ownership, dilution, round leadership, conditions, use of funds, runway and expected follow-on capital. Summarize previous rounds and total capital raised. Provide sources-and-uses and capitalization tables when inputs exist. Recalculate implied ownership and valuation, state formulas, and flag mismatches. List every missing commercial or legal term that prevents an investment decision, including liquidation preference, conversion, anti-dilution, governance, information, exit and reserved-matter rights.
""".strip(),
    "Key Financials": """
Perform a detailed financial review rather than reproducing headline numbers. Build period-by-period tables with units and clearly label actual, annualized, budget and forecast figures. Cover revenue and its product, customer, channel and geography mix; volume and pricing; gross and contribution profit; employee, marketing and other operating costs; EBITDA and margin; exceptional items; cash burn and runway; working capital; receivables, inventory and payables; capex; debt; cash; balance sheet; and operating cash flow. Calculate growth, margin movement, burn multiple, runway and return ratios only from disclosed inputs, and show formulas. Explain the drivers of every material movement. Reconcile P&L, balance-sheet, cash-flow, MIS and model inconsistencies. Include sensitivities for the main forecast assumptions. Do not merge currencies, units or periods.
""".strip(),
    "Transaction / Trading Multiples": """
Create a valuation bridge and comparable-company or precedent-transaction tables using only names and figures present in the evidence. For each comparable show company or transaction, date, geography, business fit, revenue or EBITDA period, valuation basis, enterprise or equity value, multiple and source. Calculate the subject company's EV/Revenue, EV/EBITDA and other relevant multiples with formulas, consistent periods and an explicit equity-to-enterprise-value bridge. Explain why each comparable is or is not comparable and quantify premiums or discounts where possible. Separate transaction multiples from public trading multiples. Do not manufacture a peer set. If usable comparable data is absent, state exactly which inputs require external validation and still show the valuation calculations supported by internal documents.
""".strip(),
    "Risk Factors": """
Build a ranked risk register with category, specific risk, supporting evidence, leading indicator, probability, financial or operational impact, existing mitigant, proposed mitigant, owner and diligence test. Cover customer and supplier concentration, demand, pricing, gross margin, cash burn, forecast execution, working capital, key people, technology, regulation, litigation, governance, reporting quality, transaction terms and exit. Quantify downside or sensitivity where the source inputs allow it. Distinguish an observed issue from a hypothetical risk. Call out contradictions and management assumptions that lack support. Rank the issues that can stop the deal separately from risks that can be monitored after investment.
""".strip(),
    "Investment Rationale": """
Present five to ten distinct underwriting reasons, but include only rationales supported by evidence. For each one, state the thesis, cited proof, economic mechanism, durability, counterevidence, metric to monitor and diligence condition. Address growth, market position, product value, customer behavior, unit economics, margin path, management capability, capital efficiency, valuation and exit relevance where supported. Include a thesis-to-evidence table. Explain why the opportunity may be attractive now and what must be true for returns to work. Do not convert management forecasts or marketing claims into facts, and do not force a positive conclusion.
""".strip(),
    "Exit Considerations": """
Analyze plausible exit routes from the disclosed business and transaction facts. Cover strategic sale, sponsor sale, founder or investor buyback, secondary sale and IPO only where relevant. Identify named or defensible buyer categories, strategic logic, timing constraints and milestones required for each route. Show entry valuation and multiple, expected dilution, future funding, exit metric, exit multiple, stake at exit, proceeds, MOIC and IRR scenarios when all inputs are available. State every formula and separate management assumptions from analyst sensitivities. Include downside, base and upside cases without inventing missing values. Discuss liquidity, marketability, governance and exit-right constraints, then list the evidence needed to underwrite an exit.
""".strip(),
    "Next Steps": """
Turn every material uncertainty from the report into an executable diligence plan. Provide a prioritized table with serial number, question or risk, exact action, why it matters, required document or evidence, owner, assignee, status, target date or timing, dependency and expected output. Separate pre-IC gating work, confirmatory diligence, legal or transaction work and post-investment monitoring. Include financial reconciliations, customer and supplier calls, market validation, management references, technology or operations review, legal and regulatory checks, cap-table verification and transaction-term negotiation when relevant. Tie each task to a specific unresolved claim, contradiction or source gap. Do not use generic actions such as 'review documents' without naming the document, test and decision it informs.
""".strip(),
}
