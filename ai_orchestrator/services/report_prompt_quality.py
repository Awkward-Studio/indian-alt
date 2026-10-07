"""Align authored IC prompts with the application's verified citation contract."""
from __future__ import annotations

import re


CITATION_GUIDANCE = (
    "Cite every factual statement, number, date, management claim and supported table "
    "row/cell with an exact supplied retrieval marker, for example [R001]. "
    "For two sources write [R001] [R002]. A filename, [IM: filename], [EXT: filename] "
    "or a source-type label alone is not a citation. Do not replace markers with "
    "filenames or create your own numeric reference list. The application converts "
    "markers into numbered citations and links to the supplied source. "
    "For a narrower spreadsheet location use [R020@'Revenue Build'!F42:H42] only "
    "within that block's verified bounds. Cite dependency cells using their own "
    "supplied markers. Never invent a rank, source, cell, page or URL."
)
DEPTH_GUIDANCE = (
    "Write a thorough assessment of every material theme in the section instructions. "
    "For each material finding give the evidence, calculation or operating mechanism, "
    "counterevidence or uncertainty, and investment implication. Include the available "
    "financial schedules and explain their drivers. Concise means precise sentences, "
    "not abbreviated coverage. Do not stop at a headline summary. Where evidence is "
    "missing, identify the exact missing record and how it affects the conclusion. "
    "Do not claim completeness, reconciliation or verification that was not performed."
)
PRESENTATION_GUIDANCE = (
    "Use ### descriptive topic headings and #### for necessary subsections. "
    "Use short paragraphs, a blank line between blocks, and simple GFM tables with "
    "one cell per column and escaped literal pipes. Put metrics in rows and periods "
    "in columns, label units and actual/forecast status, and interpret each important "
    "table in prose. Split unrelated schedules into separate tables. Avoid whole "
    "paragraphs in cells, bolding entire paragraphs, raw HTML or a Markdown code fence. "
    "Keep Next Steps' twelve-column task schema unchanged. Do not author a References "
    "or Citations section; the application supplies verified source links."
)
EDITORIAL_GUIDANCE = (
    "Organize the analysis for an investment committee reader scanning a long report. "
    "Use specific ### topic headings and #### subheadings when a topic has distinct parts. "
    "Write focused paragraphs of two to four sentences, usually 40-90 words; split long "
    "paragraphs at a change of finding, evidence or implication. Bold one or two short "
    "phrases per paragraph to highlight the central finding, decision-critical metric "
    "or investment implication. Never bold entire sentences or paragraphs or every "
    "number. Use italics sparingly for assumptions, forecast qualifications or unresolved "
    "limitations; keep the evidence and reasoning in ordinary text. Use bullets for "
    "parallel findings, risks or diligence questions, with a short bold lead-in followed "
    "by its evidence and implication. Use a brief blockquote only for a material "
    "approval condition or limitation already supported in the analysis. Keep formulas "
    "and their cited inputs legible, all citations adjacent to their claims, and the "
    "single financial table and Next Steps schema intact. Preserve analytical depth; "
    "formatting must not introduce new claims or replace substantive analysis with labels."
)
REPORT_SIGNAL_GUIDANCE = (
    "When listing missing evidence or unresolved diligence gaps, begin the bullet "
    "with ⚠️ **Gap:**, name the specific missing record, and state the investment "
    "implication. When two supplied sources make incompatible claims, begin the "
    "bullet with 🚩 **Contradiction:**, describe both claims with their separate "
    "citations, and state what must resolve the conflict. For a genuine unresolved "
    "numeric mismatch use ⚡ **Number conflict:**, identify the periods, units and "
    "source values before drawing conclusions. Keep these icons attached to their "
    "labeled findings, not ordinary bullets or section headings. Do not manufacture "
    "gaps or contradictions for decoration, or label your own arithmetic error a "
    "source contradiction. Preserve the existing Next Steps table schema."
)
CALCULATION_GUIDANCE = (
    "Use the exact indexed source values and their own period, units and reporting "
    "basis for financial claims in every section. Use the recorded financial "
    "statement rows when supplied; prior generated analysis is not an authoritative "
    "replacement for workbook cells. One crore is 10,000,000 rupees, one lakh is "
    "100,000, one million is 1,000,000 and one thousand is 1,000. Show conversions "
    "and preserve signs. CAGR = (ending / starting)^(1 / elapsed years) - 1; count "
    "elapsed annual intervals, not the number of observations. MOIC = investor "
    "exit proceeds / invested capital. For one initial investment and one exit, "
    "annual IRR = MOIC^(1 / holding years) - 1. Multiple dated cash flows need "
    "a dated cash-flow calculation, not that shortcut. Cite inputs and label "
    "assumptions. Match every scenario table to its calculated prose, currency "
    "scale, stake and holding period. Never describe negative source EBITDA as "
    "positive; keep management-adjusted figures separate and reconcile their "
    "adjustments. Check numerical assertions throughout the narrative, including "
    "people costs, fees, valuations, margins, sensitivities and returns. Where "
    "a value cannot be calculated from supported inputs, state Not provided and "
    "identify the missing input instead of fabricating a result."
)
GENERATION_GUIDANCE = (
    "Before drafting, organize the section's required analytical themes against "
    "the supplied primary retrieval blocks. For each material claim distinguish "
    "documented facts, management claims, projections, explicitly labeled assumptions "
    "and analytical conclusions. A current cap table does not prove a proposed "
    "round's primary/secondary split; a funding ask is not enterprise value; cash "
    "balances alone do not prove an ability to self-fund. Keep unavailable terms "
    "and records as explicit gaps with a concrete diligence action. Use the exact "
    "worksheet's period and unit headers for each number. Build derived calculations "
    "from cited primary inputs and use the available calculator before committing "
    "their results to prose or tables. Distinguish unadjusted from adjusted metrics "
    "and identify missing adjustment bridges. Do not fill unsupported classifications "
    "or expense forecasts with estimates to complete a table. Reassess previous "
    "draft feedback against the current primary evidence. "
    "Preserve genuine corrections, and cover unavailable information with a labeled "
    "gap and diligence action. Do not manufacture facts to satisfy a feedback item. "
    "Before returning the "
    "section, check that every requested analytical theme is covered, every material "
    "claim has the correct evidence marker, every table agrees with its prose, and "
    "calculated figures preserve source precision and signs. Keep this preparation "
    "and final check internal; return only the finished Markdown section."
)
SECTION_OWNERSHIP = (
    "Keep each calculation and detailed schedule in its owning section. Company Details "
    "owns operating model, products, customers and delivery capability. Promoter and "
    "Management Details owns people and governance. Industry Overview owns market "
    "structure and competitive landscape. Transaction Details owns deal terms, cap "
    "table, dilution, ownership and sources/uses calculations. Key Financials owns "
    "financial statements, forecast/formula interpretation, growth/margin/working-capital "
    "calculations, cash needs and operating sensitivities. Transaction / Trading Multiples "
    "owns comparable tables, EV bridge and valuation multiples. Risk Factors owns the "
    "ranked downside assessment, referencing financial sensitivities without repeating "
    "their calculations. Investment Rationale owns the thesis and the evidence that "
    "supports or refutes it. Exit Considerations owns exit routes, proceeds waterfall, "
    "MOIC/IRR calculations and exit sensitivities. Executive Summary owns the recommendation "
    "and a brief scorecard, referring to the detailed sections. Next Steps owns the "
    "consolidated diligence action register. Use exact section titles for cross-references. "
    "Where prior sections are supplied, reuse their supported canonical values and flag "
    "conflicts rather than recalculating a different result. A repeated headline needed "
    "to explain a new investment implication is allowed; repeating a paragraph, schedule, "
    "biography or calculation is not. Do not imply a prior section was reviewed if absent."
)
FINAL_CHECK = (
    "\n\n- Final output check: cover every material analytical theme above; "
    "cite source-backed claims and table rows with exact [Rnnn] markers from the "
    "evidence, not filename-only labels; meet minimum_words={{ minimum_words }} "
    "in your analysis excluding source labels/references and aim for "
    "target_words={{ target_words }} when evidence supports it. Keep the exact "
    "section heading, readable subheadings, tables and concrete unresolved actions."
)


def upgrade_report_prompt(system: str, user: str, *, section_title: str = "") -> tuple[str, str]:
    """Preserve all business instructions and template variables; replace contradictions."""
    system = system.replace(
        "Be skeptical, commercially practical and concise.",
        "Be skeptical, commercially practical, precise and thorough.",
    )
    lines = []
    for line in system.splitlines():
        if line.startswith("Tag every factual claim"):
            lines.append(CITATION_GUIDANCE + " Mark material missing evidence as ⚠️ GAP. "
                         "Flag direct contradictions with 🚩 and unresolved numeric mismatches "
                         "with ⚡ NUMBER CONFLICT; distinguish dates, units and reporting basis.")
        elif line.startswith("- Cite every factual statement"):
            lines.append("- " + CITATION_GUIDANCE)
        elif line.startswith("- Preserve exact supplied"):
            continue
        elif "Retain >> [EXT:" in line:
            lines.append(line.split(" Retain >>", 1)[0] +
                         " Mark supplied external research in a blockquote beginning "
                         "**External evidence:** and keep its [Rnnn] markers. Do not inject HTML.")
        else:
            lines.append(line)
    system = "\n".join(lines).rstrip()
    if "Report depth and layout:" not in system:
        system += "\n\nReport depth and layout:\n- " + DEPTH_GUIDANCE + "\n- " + PRESENTATION_GUIDANCE
    if "Section ownership and cross-references:" not in system:
        system += "\n\nSection ownership and cross-references:\n" + SECTION_OWNERSHIP
    if "Report reading hierarchy:" not in system:
        system += "\n\nReport reading hierarchy:\n" + EDITORIAL_GUIDANCE
    # A pipeline's published skill may supply the system message. Keep the
    # requested report formatting in the section user prompt as well.
    if "Report reading hierarchy:" not in user:
        user = user.rstrip() + "\n\nReport reading hierarchy:\n" + EDITORIAL_GUIDANCE + "\n"
    if "Report gap and contradiction flags:" not in user:
        user = user.rstrip() + "\n\nReport gap and contradiction flags:\n" + REPORT_SIGNAL_GUIDANCE + "\n"
    if "Report calculation discipline:" not in user:
        user = user.rstrip() + "\n\nReport calculation discipline:\n" + CALCULATION_GUIDANCE + "\n"
    if "Grounded section generation:" not in user:
        user = user.rstrip() + "\n\nGrounded section generation:\n" + GENERATION_GUIDANCE + "\n"
    # Consolidate actions in one section; avoid eleven copies of the same register.
    system = re.sub(r"^- For A–I, end with an action table.*$",
        "- State section-specific unresolved issues in concise prose or bullets. "
        "Next Steps owns the consolidated action table in its specified twelve-column schema. "
        "The Executive Summary states the material approval gates.", system, flags=re.MULTILINE)
    is_financials = section_title == "Key Financials" or "## Key Financials" in user
    if is_financials and "Key Financials table format:" not in user:
        from ai_orchestrator.services.report_financial_format import FINANCIAL_TABLE_INSTRUCTION
        user = user.replace(
            "- Begin with period-by-period financial tables, metrics in rows and periods in columns,",
            "- Present the single standardized P&L table specified below, metrics in rows and periods in columns,",
        )
        user = user.replace("Operating Cash Flow and Cash rows where supported", "Operating Cash Flow and Cash findings in prose where supported")
        user = FINANCIAL_TABLE_INSTRUCTION.lstrip() + "\n\n" + user
    if is_financials and "Key Financials table format:" not in system:
        from ai_orchestrator.services.report_financial_format import FINANCIAL_TABLE_INSTRUCTION
        system += FINANCIAL_TABLE_INSTRUCTION
    if is_financials and 'Financial currency and display precision:' not in user:
        precision = ('\n\nFinancial currency and display precision:\n'
            'State currency and scale in the table header (for example Metric (INR Cr, amounts)). '
            'Revenue-to-PAT rows contain monetary amounts, never percentages. Display amounts to exactly '
            'two decimal places; calculate using unrounded saved source values before displaying results. '
            'Show growth and margins with %, multiples with x, and working-capital duration with days. '
            'Use native model units; currency/scale Not provided if primary sources do not state them. '
            'Do not use generated summaries or implied-by-scale guesses to override a worksheet header.\n')
        user += precision
        system += precision
    user = re.sub(
        r"- Provide the analytical depth supported by the evidence\. Runtime guidance:.*"
        r"Do not add repetition or unsupported claims to meet a length target\.",
        "- Cover every material theme above in depth. Write at least {{ minimum_words }} "
        "words of substantive analysis, excluding citations/source labels, and aim for "
        "{{ target_words }} words when the evidence supports it. Explain mechanisms, "
        "supported calculations, sensitivities, counterevidence and investment implications. "
        "Avoid repetition and unsupported claims; make missing evidence actionable.",
        user,
    )
    if "- Final output check:" not in user:
        user = user.rstrip() + FINAL_CHECK + "\n"
    if is_financials and "Financial table citation rule:" not in user:
        user += "\nFinancial table citation rule: Every row containing reported or calculated numbers "
        user += "must include the exact supplied [Rnnn] markers INSIDE that table row, in its metric "
        user += "label or value cells. A citation in a paragraph below the table does not cite the row. "
        user += "For example, | Revenue [R001] | 100 | 120 |. Use your actual evidence markers and "
        user += "values, not this example's numbers. If periods use different sources, cite the relevant "
        user += "cells separately. Cite all source inputs for calculated rows. Rows entirely marked Not "
        user += "provided need no citation. Final check: exactly one table, exactly the 13 specified "
        user += "rows from Revenue to PAT, and verified markers inside every numeric row.\n"
    if is_financials and "Financial bridge check:" not in user:
        from ai_orchestrator.services.report_financial_format import FINANCIAL_BRIDGE_RULE
        user += FINANCIAL_BRIDGE_RULE
    if is_financials and "Financial source and period check:" not in user:
        from ai_orchestrator.services.report_financial_format import FINANCIAL_SOURCE_RULE
        user += FINANCIAL_SOURCE_RULE
    return system.rstrip(), user
