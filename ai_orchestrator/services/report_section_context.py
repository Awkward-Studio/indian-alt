"""Provide completed sections for reconciliation without consuming the evidence budget."""
import re

from ai_orchestrator.services.token_budget import estimate_tokens


def completed_report_sections(metadata: dict, title: str, *, fallback_report='') -> dict:
    from ai_orchestrator.services.report_sections import ICReportSectionService
    completed = ICReportSectionService._split(fallback_report) if title == 'Executive Summary' else {}
    completed.update({item.get('title'): str(item.get('content') or '')
        for item in metadata.get('report_section_queue') or []
        if item.get('status') == 'completed' and item.get('content') and item.get('title') != title})
    completed.pop(title, None)
    return completed


def prior_section_context(metadata: dict, title: str, *, max_tokens=6_000, fallback_report='') -> str:
    if title in {"Key Financials", "Transaction Details"}:
        return ""
    completed = completed_report_sections(metadata, title, fallback_report=fallback_report)
    if not completed:
        return ""
    preferred = ["Key Financials", "Transaction Details", "Transaction / Trading Multiples"]
    if title in {"Executive Summary", "Next Steps", "Risk Factors", "Investment Rationale", "Exit Considerations"}:
        preferred += [key for key in completed if key not in preferred]
    blocks = ["Completed sections from this report, supplied only for reconciliation and cross-references. "
              "Reuse their supported canonical values; put new analysis in this section's remit. "
              "These are analyst drafts, not new primary evidence. Cite claims using this request's "
              "supplied [Rnnn] evidence markers; never copy another section's citation numbers."]
    if title == 'Executive Summary':
        from .executive_summary_format import EXECUTIVE_SUMMARY_REPORT_BASIS
        blocks = [EXECUTIVE_SUMMARY_REPORT_BASIS + '\n\nCompleted or latest saved report sections. '
                  'Use these reports as the basis for the summary; cite their facts using matching '
                  'current evidence markers rather than copying their old citation numbers.']
    for key in preferred:
        if key not in completed:
            continue
        body = re.split(r"^###\s+Citations\s*$", completed[key], maxsplit=1, flags=re.M)[0]
        body = re.sub(r"\[\d+\]", "", body)
        # Executive Summary needs every completed report in full. Its caller
        # reserves this context before allocating primary-evidence tokens.
        # Other sections receive bounded reconciliation excerpts.
        if title != 'Executive Summary' and key not in {"Key Financials", "Transaction Details", "Transaction / Trading Multiples"}:
            paragraphs = body.split("\n\n")
            body = "\n\n".join(paragraphs[:3] + paragraphs[-3:]) if len(paragraphs) > 6 else body
        if title != 'Executive Summary' and estimate_tokens("\n\n".join([*blocks, body])) > max_tokens:
            # Keep whole blocks; never truncate a financial table in mid-row.
            summary = "\n\n".join(body.split("\n\n")[:2])
            if estimate_tokens("\n\n".join([*blocks, summary])) <= max_tokens:
                blocks.append(summary)
            continue
        blocks.append(body)
    return "\n\n".join(blocks) if len(blocks) > 1 else ""
