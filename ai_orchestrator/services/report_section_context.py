"""Provide completed sections for reconciliation without consuming the evidence budget."""
import re

from ai_orchestrator.services.token_budget import estimate_tokens


def prior_section_context(metadata: dict, title: str, *, max_tokens=6_000) -> str:
    if title in {"Key Financials", "Transaction Details"}:
        return ""
    completed = {item.get("title"): str(item.get("content") or "")
        for item in metadata.get("report_section_queue") or []
        if item.get("status") == "completed" and item.get("content") and item.get("title") != title}
    if not completed:
        return ""
    preferred = ["Key Financials", "Transaction Details", "Transaction / Trading Multiples"]
    if title in {"Executive Summary", "Next Steps", "Risk Factors", "Investment Rationale", "Exit Considerations"}:
        preferred += [key for key in completed if key not in preferred]
    blocks = ["Completed sections from this report, supplied only for reconciliation and cross-references. "
              "Reuse their supported canonical values; put new analysis in this section's remit. "
              "These are analyst drafts, not new primary evidence. Cite claims using this request's "
              "supplied [Rnnn] evidence markers; never copy another section's citation numbers."]
    for key in preferred:
        if key not in completed:
            continue
        body = re.split(r"^###\s+Citations\s*$", completed[key], maxsplit=1, flags=re.M)[0]
        body = re.sub(r"\[\d+\]", "", body)
        # Financial/transaction schedules govern numbers; prose summaries plus
        # final unresolved issues keep the remaining section drafts bounded.
        if key not in {"Key Financials", "Transaction Details", "Transaction / Trading Multiples"}:
            paragraphs = body.split("\n\n")
            body = "\n\n".join(paragraphs[:3] + paragraphs[-3:]) if len(paragraphs) > 6 else body
        if estimate_tokens("\n\n".join([*blocks, body])) > max_tokens:
            # Keep whole blocks; never truncate a financial table in mid-row.
            summary = "\n\n".join(body.split("\n\n")[:2])
            if estimate_tokens("\n\n".join([*blocks, summary])) <= max_tokens:
                blocks.append(summary)
            continue
        blocks.append(body)
    return "\n\n".join(blocks) if len(blocks) > 1 else ""
