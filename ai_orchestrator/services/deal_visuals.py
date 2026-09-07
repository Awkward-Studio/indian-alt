"""Shared rendering contract applied after configurable deal-chat prompts."""

DEAL_VISUAL_OUTPUT_CONTRACT = """[VISUAL OUTPUT]
Choose the answer format from the question and the available evidence, even when the user does not explicitly say chart.
Use a line chart for growth or trends over comparable periods, a bar chart for numeric category comparisons, a pie or donut for composition of a whole, and KPI cards for a headline snapshot. Use a timeline for milestones.
When the user asks for a graph, chart, visual, infographic, timeline, KPI view, comparison, or financial deep dive, include fenced deal_visual JSON blocks when the available evidence supports them.
Respect an explicit request for a table or text-only answer. Simple factual answers and qualitative explanations do not need visuals.
A comparison_matrix is a table, not a chart: do not use it to satisfy an explicit graph or chart request. Prefer charts for comparable numeric metrics; reserve matrices for mixed qualitative attributes or detailed exact-value lookup.
Avoid repeating a chart's full dataset in a Markdown table unless the user asks for both.
Return one visual for a singular request. Return up to three distinct visuals when the user asks for charts/graphs, multiple visuals, or a deep dive and the evidence supports materially different views.
Put each visual in its own fenced deal_visual block. Do not repeat the same values in multiple visuals merely to reach the limit.
Do not invent values for a visual. Use a supported subset when it answers the question, state its coverage and missing evidence, and never fill gaps with zeros. If no meaningful supported visual is possible, explain what is missing.
Copy every numeric value at the exact scale stated in the evidence: 214 must remain 214, not 21.4 or 2140. Never rescale, normalize, annualize, interpolate, or convert a value unless the evidence explicitly provides that converted value.
Each visual block must be valid JSON only, with no comments or trailing commas.
Every visual object MUST include `"version": 1`, a supported `type`, a non-empty `title`, and a non-empty `data` array.
Always emit `source_notes` as an array of strings, even when there is only one source. Never emit it as a single string.
Example skeleton: {"version": 1, "type": "bar", "title": "Revenue trend", "summary": "Revenue increased.", "unit": "INR Cr", "data": [{"label": "FY25", "value": 100}], "source_notes": ["Information memorandum, page 26"]}
Supported type values are: bar, line, area, pie, donut, kpi_strip, timeline, comparison_matrix.
Choose the type from the shape of the evidence:
- line: a chronological trend with at least two comparable numeric periods. When the labels are dates, fiscal years, quarters, or months for the same metric, use line rather than bar unless the user explicitly requests bars.
- area: a chronological magnitude or cumulative trend with at least two comparable numeric periods.
- bar: one comparable numeric measure across categories, companies, business units, or periods.
- pie or donut: non-negative parts of one whole, all measured in the same unit. Do not use these for unrelated KPIs.
- kpi_strip: a point-in-time snapshot of heterogeneous headline metrics with different units. Do not choose it when a trend, composition, or category comparison is available and more informative.
- timeline: dated or sequential milestones, transactions, or risks.
- comparison_matrix: several metrics compared across two or more companies, scenarios, or periods.
For bar, line, area, pie, and donut, data rows must use {"label": "...", "value": 123.4}. Values must be JSON numbers; put the shared unit in the top-level `unit` field.
Use concise source_notes that identify the supporting document or context. Wrap every visual with a short Markdown explanation before or after it.
For kpi_strip rows use {"label":"Revenue", "value":100, "unit":"INR Cr", "tone":"neutral"}.
For timeline rows use {"label":"Investment", "date":"2025", "description":"Transaction completed", "tone":"neutral"}.
For comparison_matrix rows use {"label":"Revenue", "values":{"Company A":"100 INR Cr", "Company B":"120 INR Cr"}}.
The chart renderer supports one series per chart. For multiple metrics, emit separate charts with their own units; never interleave series in a single line or area chart.
Before finishing, check that any requested chart is present as a complete deal_visual fence, uses a supported schema, and contains only evidence-backed values. If a chart cannot be supported, explain why in the answer.
"""


def apply_deal_visual_contract(system_instructions: str, pipeline_key: str, stage_key: str) -> str:
    if (pipeline_key, stage_key) != ("deal_chat", "answer"):
        return system_instructions
    return system_instructions + "\n\n[DEAL CHAT RENDERING CONTRACT]\nThese rules govern visual formatting for this chat response.\n" + DEAL_VISUAL_OUTPUT_CONTRACT
