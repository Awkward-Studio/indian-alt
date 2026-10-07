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


def upgrade_report_prompt(system: str, user: str) -> tuple[str, str]:
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
    # Consolidate actions in one section; avoid eleven copies of the same register.
    system = re.sub(r"^- For A–I, end with an action table.*$",
        "- State section-specific unresolved issues in concise prose or bullets. "
        "Next Steps owns the consolidated action table in its specified twelve-column schema. "
        "The Executive Summary states the material approval gates.", system, flags=re.MULTILINE)
    if "## Key Financials" in user and "Key Financials table format:" not in user:
        from ai_orchestrator.services.report_financial_format import FINANCIAL_TABLE_INSTRUCTION
        user = user.replace(
            "- Begin with period-by-period financial tables, metrics in rows and periods in columns,",
            "- Present the single standardized P&L table specified below, metrics in rows and periods in columns,",
        )
        user = user.replace("Operating Cash Flow and Cash rows where supported", "Operating Cash Flow and Cash findings in prose where supported")
        user = FINANCIAL_TABLE_INSTRUCTION.lstrip() + "\n\n" + user
    if "## Key Financials" in user and "Key Financials table format:" not in system:
        from ai_orchestrator.services.report_financial_format import FINANCIAL_TABLE_INSTRUCTION
        system += FINANCIAL_TABLE_INSTRUCTION
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
    return system, user
