"""Replace the main financial statement while preserving the surrounding Markdown."""
import re

from ai_orchestrator.services.report_financial_format import FINANCIAL_ROWS
from ai_orchestrator.services.report_sections import ICReportSectionService


def locate_main_financial_table(markdown):
    tables = []
    for match in re.finditer(r'^[ \t]*\|[^\n]*(?:\n[ \t]*\|[^\n]*)+', markdown, re.M):
        lines = match[0].splitlines()
        if len(lines) < 3 or not ICReportSectionService._is_table_separator(lines[1]):
            continue
        labels = [re.sub(r'[^a-z0-9]', '', re.sub(r'\[[^]]+\]', '',
            ICReportSectionService._plain_table_label(ICReportSectionService._table_cells(line)[0])).casefold()) for line in lines[2:]]
        tables.append((match, labels))
    expected = [re.sub(r'[^a-z0-9]', '', label.casefold()) for label in FINANCIAL_ROWS]
    candidates = [match for match, labels in tables if labels == expected]
    if not candidates:
        candidates = [match for match, labels in tables if {'revenue', 'pat'}.issubset(labels)]
    if len(candidates) != 1:
        raise ValueError('A single main Revenue-to-PAT table is required for a table rewrite.')
    match = candidates[0]
    return match[0], match.start(), match.end()


def merge_financial_table(original, generated):
    from .report_assembly import strip_report_review_blocks
    original, generated = strip_report_review_blocks(original), strip_report_review_blocks(generated)
    table, _, _ = locate_main_financial_table(generated)
    _, start, end = locate_main_financial_table(original)
    # New retrieval references have their own numbering. Keep old references
    # intact and allocate unused numbers to the replacement table's sources.
    old_numbers = [int(value) for pair in re.findall(r'\[(\d+)\]|^\s*(\d+)\.\s', original, re.M) for value in pair if value]
    offset = max(old_numbers, default=0)
    refs = re.split(r'^###\s+Citations\s*$', generated, maxsplit=1, flags=re.M)
    references = refs[1].strip() if len(refs) == 2 else ''
    table = re.sub(r'\[(\d+)\]', lambda m: f'[{int(m[1])+offset}]', table)
    references = re.sub(r'^(\s*)(\d+)(\.\s)', lambda m: f'{m[1]}{int(m[2])+offset}{m[3]}', references, flags=re.M)
    updated = original[:start] + table + original[end:]
    if references:
        heading = '' if re.search(r'^###\s+Citations\s*$', original, re.M) else '### Citations\n\n'
        updated = updated.rstrip() + '\n\n' + heading + references + '\n'
    return updated, table
