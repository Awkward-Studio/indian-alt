"""Derive the summary chart table from this report's saved main P&L."""
import re
from decimal import Decimal

from ai_orchestrator.services.report_financial_format import displayed_amount
from ai_orchestrator.services.report_sections import ICReportSectionService
from deals.services.financial_table_rewrite import locate_main_financial_table


def align_summary_financials(summary, key_financials):
    if not key_financials:
        return summary
    try:
        table, _, _ = locate_main_financial_table(key_financials)
    except ValueError:
        return summary
    rows = [ICReportSectionService._table_cells(line) for line in table.splitlines()]
    headers = rows[0]
    metrics = {ICReportSectionService._plain_table_label(row[0]).casefold(): row[1:] for row in rows[2:]}
    revenue, ebitda = metrics.get('revenue'), metrics.get('ebitda')
    if not revenue or not ebitda or len(revenue) != len(headers)-1 or len(ebitda) != len(revenue):
        return summary
    match = re.search(r'^#### Financial performance\s*\n(.*?)(?=^#### Key metrics\s*$)', summary, re.M | re.S)
    if not match:
        return summary
    used = [int(number) for pair in re.findall(r'\[(\d+)\]|^\s*(\d+)\.\s', summary, re.M) for number in pair if number]
    offset = max(used, default=0)
    source_number = offset+1
    # Cite the saved report explicitly. Its own row references and primary
    # source footnotes remain in Key Financials, rather than being renumbered
    # incorrectly into this section's independent retrieval citations.
    revenue_cells, margin_cells = [], []
    for revenue_cell, ebitda_cell in zip(revenue, ebitda):
        rv, ev = displayed_amount(revenue_cell), displayed_amount(ebitda_cell)
        revenue_cells.append(f'{rv[0]:f} [{source_number}]' if rv else 'Not provided')
        margin = ev[0] / rv[0] * 100 if rv and rv[0] > 0 and ev else None
        margin_cells.append(f'{margin.quantize(Decimal("0.01")):f}% [{source_number}]' if margin is not None else 'Not provided')
    basis = re.sub(r'\[[^]]+\]|[*`]', '', headers[0]).strip()
    projected = ('#### Financial performance\n\n'
        f'Revenue uses the same monetary units and reporting basis as Key Financials: {basis}. '
        'Period labels are copied unchanged. EBITDA margin = same-period EBITDA / Revenue × 100. '
        'Negative EBITDA produces a negative margin.\n\n'
        + '| Metric | '+' | '.join(headers[1:])+' |\n'
        + '| '+' | '.join(['---']*len(headers))+' |\n'
        + '| Revenue | '+' | '.join(revenue_cells)+' |\n'
        + '| EBITDA margin | '+' | '.join(margin_cells)+' |\n\n')
    updated = summary[:match.start()] + projected + summary[match.end():]
    if not re.search(r'^### Citations\s*$', updated, re.M):
        updated = updated.rstrip()+'\n\n### Citations\n\n'
    return updated.rstrip()+f'\n{source_number}. Saved Key Financials report, Main Standardized P&L; same report version. Primary source references are retained beside the Revenue and EBITDA cells in Key Financials.\n'
