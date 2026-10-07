"""Share the accepted Key Financials table with the target financial profile."""
import re
from decimal import Decimal
from ai_orchestrator.services.report_financial_format import FINANCIAL_ROWS, displayed_amount
from deals.services.financial_evidence import reported_unit

ROW_KEYS = ['revenue', 'cogs', 'gross_profit', 'expenses', 'ebitda', 'depreciation', 'ebit',
            'interest', 'other_income', 'exceptional_items', 'profit_before_tax', 'tax', 'pat']

def section_payload(section, source_id):
    blocks = re.findall(r'(?:^|\n)(\|[^\n]+\|(?:\n\|[^\n]+\|)+)', section)
    tables = [[[cell.strip() for cell in line.strip().strip('|').split('|')] for line in block.splitlines()] for block in blocks]
    tables = [table for table in tables if [re.sub(r'\[\d+\]|[*_]', '', row[0]).strip() for row in table[2:]] == list(FINANCIAL_ROWS)]
    if len(tables) != 1:
        raise ValueError('Accepted Key Financials must contain one main Revenue-to-PAT statement; supplemental tables are allowed.')
    rows = tables[0]
    labels = [re.sub(r'\[\d+\]|[*_]', '', row[0]).strip() for row in rows[2:]]
    if labels != list(FINANCIAL_ROWS) or any(len(row) != len(rows[0]) for row in rows):
        raise ValueError('Key Financials table does not match the Revenue-to-PAT contract.')
    header = rows[0][0]
    unit = None if re.search(r'native|not provided', header, re.I) else reported_unit(header)
    basis = 'Consolidated' if re.search(r'\bconsolidated\b', header, re.I) else 'Standalone'
    citations = {}
    evidence = {}
    statements = []
    for column, period in enumerate(rows[0][1:], 1):
        # The profile stores one amount per fiscal period, not scenario bounds.
        # Keep ranges in the report rather than silently selecting an endpoint.
        if re.search(r'\b(?:lower|upper) bound\b', period, re.I):
            continue
        match = re.search(r'\bFY\s*(20\d{2}|\d{2})(?:[-/](20\d{2}|\d{2}))?\s*([AEFP])?\b', period, re.I)
        if not match:
            continue
        year = int(match[2] or match[1]) % 100
        forecast = (match[3] or '').upper() in {'E', 'F', 'P'} or bool(re.search(r'forecast|projected|estimate', period, re.I))
        fy = f'FY{year:02d}' + ('E' if forecast else '')
        metrics = {}
        for key, row in zip(ROW_KEYS, rows[2:]):
            parsed = displayed_amount(row[column])
            if parsed is None:
                continue
            references = re.findall(r'\[(\d+)\]', row[column]) or re.findall(r'\[(\d+)\]', row[0])
            if not references:
                raise ValueError(f'Key Financials {key} in {fy} has no source citation.')
            value = format(parsed[0], 'f') + (' '+unit if unit else '')
            refs = [f'R{int(number):03d}' for number in references]
            metrics[key] = {'value': value, 'evidence_refs': refs}
            for ref in refs:
                citations[ref] = {'document_id': str(source_id), 'title': 'Accepted Key Financials',
                                 'source_audit_id': str(source_id), 'locator': {'kind': 'reviewed_key_financials'},
                                 'report_citation_number': int(ref[1:])}
                evidence[ref] = evidence.get(ref, '') + '\n' + value
        # Derived margins use the very same accepted period and reporting units.
        revenue = displayed_amount(rows[2][column])
        for key, offset in [('gross_margin', 4), ('ebitda_margin', 6)]:
            profit = displayed_amount(rows[offset][column])
            if revenue and revenue[0] > 0 and profit:
                ratio = profit[0] / revenue[0] * Decimal(100)
                refs = list(dict.fromkeys(metrics['revenue']['evidence_refs'] + metrics[ROW_KEYS[offset-2]]['evidence_refs']))
                metrics[key] = {'value': format(ratio.quantize(Decimal('.01')), 'f')+'%', 'evidence_refs': refs}
                for ref in refs:
                    evidence[ref] += '\n'+metrics[key]['value']
        if metrics:
            statements.append({'statement_type': 'profit_loss', 'fy': fy, 'fin_type': basis, 'metrics': metrics})
    if not statements:
        raise ValueError('Key Financials contains no supported fiscal-period values.')
    return {'profile': {}, 'financial_statements': statements}, citations, evidence

def sync_section(deal, section, source_id):
    from deals.services.internal_financial_profile import InternalFinancialProfileService
    payload, citations, evidence = section_payload(section, source_id)
    result = InternalFinancialProfileService().persist(deal=deal, payload=payload, citations=citations, evidence_by_ref=evidence)
    return {**result, 'source': 'accepted_key_financials', 'source_audit_id': str(source_id)}

def latest_accepted_section(deal):
    from ai_orchestrator.models import AIAuditLog
    from django.db.models import Q
    parents = AIAuditLog.objects.filter(source_id=str(deal.id), source_metadata__queue_kind='report').exclude(source_metadata__queue_state='cancelled').order_by('-created_at')[:10]
    for parent in parents:
        item = next((item for item in (parent.source_metadata or {}).get('report_section_queue', [])
                     if item.get('title') == 'Key Financials' and item.get('status') == 'completed' and item.get('content')), None)
        if item and AIAuditLog.objects.filter(source_id=str(parent.id), source_type='vdr_report_section', status='COMPLETED',
                source_metadata__report_section='Key Financials').filter(
                Q(source_metadata__generation_mode='grounded_single_pass') |
                Q(source_metadata__report_source_review__status='no_material_error_found')).exists():
            return item['content'], str(parent.id)
    return None
