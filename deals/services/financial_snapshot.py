"""Lossless, source-native financial facts and reproducible statement snapshots.

Model prompts are consumers of this inventory, never its storage boundary.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import date

CONTRACT = 'financial-snapshot-v1'
METRICS = {
    'revenue': ('revenue', 'revenues', 'net sales', 'net revenue', 'revenue from operations', 'total revenue', 'sales', 'turnover'),
    'cogs': ('cost of goods sold', 'cost of revenue', 'cogs'),
    'gross_profit': ('gross profit', 'gross income'),
    'expenses': ('operating expenses', 'total operating expenses', 'opex'),
    'ebitda': ('ebitda',), 'depreciation': ('depreciation', 'depreciation and amortization', 'depreciation & amortisation', 'depreciation & amortization', 'd&a'),
    'ebit': ('ebit', 'operating profit'), 'interest': ('finance costs', 'finance cost', 'interest expense', 'interest expenses', 'net finance costs'),
    'other_income': ('other income', 'interest income', 'other non-operating income / expenses'),
    'exceptional_items': ('exceptional items',), 'profit_before_tax': ('pbt', 'ebt', 'profit before tax', 'earnings before tax'),
    'tax': ('income tax expense', 'tax expense', 'income tax', 'tax', 'taxes'), 'pat': ('pat', 'eat', 'profit after tax', 'net profit', 'earnings after tax'),
    'operating_cash_flow': ('cash flow from operating activities', 'net cash from operating activities', 'operating cash flow', 'cfo'),
    'investing_cash_flow': ('cash flow from investing activities', 'net cash from investing activities', 'cfi'),
    'financing_cash_flow': ('cash flow from financing activities', 'net cash from financing activities', 'cff'),
    'cash': ('cash and cash equivalents', 'cash & cash equivalents', 'closing cash', 'cash balance'),
    'debt': ('total debt', 'borrowings', 'total borrowings'), 'equity': ('total equity', 'shareholders equity', 'net worth'),
    'assets': ('total assets',), 'liabilities': ('total liabilities',),
    'receivables': ('trade receivables', 'accounts receivable', 'receivables'),
    'payables': ('trade payables', 'accounts payable', 'payables'), 'inventory': ('inventory', 'inventories'),
    'capex': ('capital expenditure', 'capex'), 'cm1': ('cm1',), 'cm2': ('cm2',), 'cm3': ('cm3',),
}
PNL_KEYS = tuple(list(METRICS)[:13])


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(',', ':')).encode()).hexdigest()


def clean(value):
    return re.sub(r'[^a-z0-9]', '', str(value).lower())


ALIASES = {clean(label): key for key, labels in METRICS.items() for label in labels}


def decimal(value):
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().replace(',', '')
    if text.startswith('(') and text.endswith(')'):
        text = '-' + text[1:-1]
    try:
        result = Decimal(text)
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def period_from_headers(headers):
    """The nearest explicit subperiod wins; parent FY never turns Q1 into annual."""
    values = [str(v).strip() for v in headers]
    joined = ' '.join(values)
    years = list(re.finditer(r'\b(?:FY|CY|AY)\s*(20\d{2}|\d{2})(?:[-/](20\d{2}|\d{2}))?\s*([AEFP])?\b', joined, re.I))
    year = int(years[-1][2] or years[-1][1]) % 100 if years else None
    if year is None:
        ranges = list(re.finditer(r'\b(20\d{2})[-/](20\d{2}|\d{2})\b',joined))
        if ranges: year=int(ranges[-1][2]) % 100
        else:
            plain_years=[int(value) % 100 for value in values if re.fullmatch(r'20\d{2}',value)]
            if plain_years: year=plain_years[-1]
    quarters = list(re.finditer(r'\bQ([1-4])\s*(?:(?:FY)?\s*(20\d{2}|\d{2}))?\b', joined, re.I))
    halves = list(re.finditer(r'\bH([12])\s*(?:(?:FY)?\s*(20\d{2}|\d{2}))?\b', joined, re.I))
    dates = []
    for value in values:
        match = re.match(r'^(20\d{2}-\d{2}-\d{2})', value)
        if match:
            try: dates.append(date.fromisoformat(match[1]))
            except ValueError: pass
    status = 'unknown'
    if re.search(r'\bactuals?\b', joined, re.I) or years and (years[-1][3] or '').upper() == 'A': status = 'actual'
    if re.search(r'\b(?:forecast|projected|projection|estimate)\b', joined, re.I) or years and (years[-1][3] or '').upper() in 'EFP' and years[-1][3]: status = 'forecast'
    if re.search(r'\bbudget\b', joined, re.I): status = 'budget'
    result = {'label': joined, 'declared_year':year if years else None, 'year': year, 'scope': 'unknown', 'status': status, 'index': None}
    if re.search(r'\bLTM\b|trailing twelve', joined, re.I): result['scope'] = 'ltm'
    elif re.search(r'\bYTD\b|year.to.date', joined, re.I): result['scope'] = 'ytd'
    elif quarters:
        q = quarters[-1]; result.update(scope='quarterly', index=int(q[1]), year=int(q[2]) % 100 if q[2] else year)
    elif halves:
        h = halves[-1]; result.update(scope='half_year', index=int(h[1]), year=int(h[2]) % 100 if h[2] else year)
    elif dates:
        d = dates[-1]; result.update(scope='monthly', index=d.month, year=year if years else (d.year + (d.month > 3)) % 100, date=d.isoformat())
        # An explicit year-end statement date is annual only when labelled so.
        if d.month == 3 and d.day == 31 and re.search(r'year ended|annual', joined, re.I): result['scope'] = 'annual'
    elif year is not None: result['scope'] = 'annual'
    result['key'] = ('FY%02d' % result['year'] if result['year'] is not None else 'Unknown') + {
        'quarterly': ':Q%s' % result['index'], 'half_year': ':H%s' % result['index'],
        'monthly': ':%s' % result.get('date', ''), 'ltm': ':LTM', 'ytd': ':YTD',
    }.get(result['scope'], '')
    return result


def units_from_labels(labels, number_format=''):
    text = ' '.join(map(str, labels))
    if '%' in number_format or any(str(label).strip() == '%' for label in labels) or re.search(r'\bmargin\s*%|\(\s*%\s*\)', text, re.I): return {'currency': None, 'scale': '0.01', 'kind': 'ratio', 'label': '%'}
    currencies = {c for c, p in [('INR', r'₹|\bINR\b|\bRs\.?\b|rupees?'), ('USD', r'\bUSD\b'), ('EUR', r'€|\bEUR\b'), ('GBP', r'£|\bGBP\b')] if re.search(p, text, re.I)}
    currency = next(iter(currencies)) if len(currencies) == 1 else None
    scale = None
    for pattern, factor in [(r'\b(?:crores?|cr)\b', '10000000'), (r'\b(?:lakhs?|lacs?)\b', '100000'), (r'\b(?:millions?|mn)\b', '1000000'), (r"\b(?:thousands?|000)\b", '1000')]:
        if re.search(pattern, text, re.I): scale = factor; break
    if scale is None and currency: scale = '1'
    return {'currency': currency, 'scale': scale, 'kind': 'amount', 'label': text}


def extract_manifest(document):
    """Retain every numeric/formula/error cell, even if not mapped to a metric."""
    from .spreadsheet_dependencies import WorkbookFormulaGraph
    from openpyxl.utils.cell import coordinate_to_tuple, range_boundaries
    manifest = getattr(document, 'extraction_manifest', None) or {}
    graph = WorkbookFormulaGraph(manifest)
    source_hash = manifest.get('content_sha256') or digest(manifest)
    facts, issues, inventory = [], [], []
    expected_sheets = (manifest.get('workbook') or {}).get('sheet_order', list(graph.by_sheet))
    if manifest.get('truncated') or set(expected_sheets) - set(graph.by_sheet):
        issues.append({'kind': 'extraction_incomplete', 'document_id': str(document.id), 'message': 'Source extraction is incomplete; missing source cells cannot be treated as absent.'})
    for sheet in graph.sheets:
        name = sheet['name']; cells = graph.by_sheet[name]
        inventory.append({'sheet': name, 'hidden': sheet.get('hidden'), 'cells': len(cells)})
        merges = []
        for bounds in sheet.get('merged_ranges', []):
            try: merges.append(range_boundaries(bounds))
            except ValueError: continue
        statement_units = [str(c.get('value')) for a, c in cells if coordinate_to_tuple(a)[0] < 15 and re.search(r'\b(?:INR|USD|EUR|GBP)\b|₹', str(c.get('value')), re.I)]
        section_labels = sorted((r, str(label)) for (sh, r), items in graph.row_labels.items() if sh == name for col, _, label in items if col <= 3 and re.search(r'^(?:B2C|B2B|Offline|Online|Marketplaces?|Website|Total channel|Channel summary|Channel mix|Overall|Consolidated)', str(label), re.I))
        numeric_rows = defaultdict(list)
        column_context = {}
        numeric_year_headers = defaultdict(list)
        date_headers = defaultdict(list)
        for address2, header_cell in cells:
            header_value = header_cell.get('cached_value') if graph.formula(header_cell) else header_cell.get('value')
            match = re.match(r'^(20\d{2}-\d{2}-\d{2})',str(header_value))
            if match:
                try: date_headers[coordinate_to_tuple(address2)[0]].append((coordinate_to_tuple(address2)[1],date.fromisoformat(match[1])))
                except ValueError: pass
        annual_date_cells=set()
        for header_row, entries in date_headers.items():
            entries=sorted(entries)
            for (left_col,left),(right_col,right) in zip(entries,entries[1:]):
                if right_col==left_col+1 and right.year==left.year+1 and (right.month,right.day)==(left.month,left.day):
                    annual_date_cells.update({(header_row,left_col),(header_row,right_col)})
        for address2, header_cell in cells:
            header_row, header_col = coordinate_to_tuple(address2)
            if header_row >= 15: continue
            header_value = header_cell.get('cached_value') if graph.formula(header_cell) else header_cell.get('value')
            if isinstance(header_value,int) and 2000<=header_value<=2100: numeric_year_headers[header_col].append((header_row,str(header_value)))
        for address, cell in cells:
            row, col = coordinate_to_tuple(address)
            raw = cell.get('cached_value') if graph.formula(cell) else cell.get('value')
            if decimal(raw) is not None or graph.formula(cell) or cell.get('data_type') == 'e': numeric_rows[row].append((address, col, cell, raw))
        for row, entries in numeric_rows.items():
            labels = graph.row_labels.get((name, row), [])
            for address, column, cell, raw in entries:
                left = [(col, label) for col, _, label in labels if col < column]
                row_unit = [label for _, label in left if re.fullmatch(r'(?:INR|USD|EUR|GBP|Rs\.?|₹)?\s*(?:Mn|Cr|million|crore|thousand|lakh|%)?', label.strip(), re.I) and label.strip()]
                metric_labels = [(col, label) for col, label in left if label not in row_unit]
                label = max(metric_labels)[1] if metric_labels else ''
                headers = [(r, label) for r, _, label in graph.column_labels.get((name, column), []) if r < row and (re.search(r'Q[1-4]|H[12]|\b(?:FY|CY|AY|YTD|LTM|actual|forecast|budget|project|year ended|annual)\b|(?:FY|CY|AY)\d|20\d{2}-\d{2}-\d{2}', label, re.I) or r < 20 and re.fullmatch(r'20\d{2}(?:[-/]\d{2,4})?[AEFP]?', label.strip(), re.I))]
                for c1, r1, c2, r2 in merges:
                    if c1 <= column <= c2 and r2 < row:
                        from openpyxl.utils import get_column_letter
                        v = (graph.cells.get((name, '%s%s' % (get_column_letter(c1), r1))) or {}).get('value')
                        if isinstance(v, str): headers.append((r1, v))
                headers.extend((header_row,header_value) for header_row,header_value in numeric_year_headers[column] if header_row<row)
                # Stop inheritance at the nearest schedule's metric/header boundary.
                headers = sorted(set(headers))[-4:]
                period = period_from_headers([v for _, v in headers])
                if period['scope']=='monthly' and any((r,column) in annual_date_cells for r,v in headers if re.match(r'20\d{2}-\d{2}-\d{2}',v)):
                    annual_year=period.get('declared_year') or int(period['date'][:4])%100
                    period.update(scope='annual',index=None,year=annual_year,key='FY%02d' % annual_year)
                if column not in column_context:
                    column_context[column] = [(r, label) for r, _, label in graph.column_labels.get((name, column), []) if re.search(r'INR|USD|EUR|GBP|₹|\b(?:Mn|Cr|million|crore|thousand|lakh)\b', label, re.I)]
                nearby_units = [v for r,v in column_context[column] if r < row] or statement_units
                units = units_from_labels(row_unit or nearby_units, cell.get('number_format', ''))
                amount = decimal(raw)
                metric = ALIASES.get(clean(label))
                if metric=='tax' and clean(label) in {'tax','taxes'}:
                    prior_labels=[str(value) for (sh,r),items in graph.row_labels.items() if sh==name and r<row for _,_,value in items]
                    if not any(ALIASES.get(clean(value)) in {'profit_before_tax','pat'} for value in prior_labels):
                        metric='unclassified_tax'
                if units['kind'] == 'ratio' and metric: metric = metric + '_margin'
                context = ' '.join([name, *[v for _, v in headers]])
                basis = 'consolidated' if re.search(r'consolidated', context, re.I) else 'standalone' if re.search(r'standalone', context, re.I) else 'unknown'
                section = next((v for r,v in reversed(section_labels) if r < row), '')
                channel_sheet = bool(re.search(r'channel|store|segment|b2[bc]|offline|marketplaces|website', name, re.I))
                entity = name if channel_sheet and clean(name) not in {'channelmix', 'channelsummary'} else 'target'
                if section and re.match(r'B2C|B2B|Offline|Online|Marketplace|Website', section, re.I): entity = name + ':' + section
                source_series = str(document.id) + ':' + name + ':' + section
                scenario = next((s for s in ['base', 'upside', 'downside'] if re.search(r'\b'+s+r'\b', context, re.I)), 'unspecified')
                state = 'supported' if amount is not None else 'missing_cache' if graph.formula(cell) else 'source_error'
                revenue_priority = 0 if re.search(r'net (?:sales|revenue)|revenue from operations|operating revenue',label,re.I) else 2 if re.search(r'total revenue',label,re.I) else 1
                fact = {'id': digest([str(document.id), source_hash, name, address]), 'document_id': str(document.id), 'source_hash': source_hash,
                        'title': str(getattr(document, 'title', '') or ''), 'locator': {'sheet': name, 'cell': address},
                        'original': str(cell.get('value')), 'value': format(amount, 'f') if amount is not None else None,
                        'revenue_priority':revenue_priority, 'formula': graph.formula(cell), 'number_format': cell.get('number_format', ''), 'label': label, 'metric': metric,
                        'period': period, 'units': units, 'basis': basis, 'entity': entity, 'scenario': scenario, 'source_series': source_series, 'state': state,
                        'provenance': 'saved_excel_cache' if graph.formula(cell) else 'source_literal'}
                facts.append(fact)
                if metric and state != 'supported': issues.append({'kind': state, 'fact_ids': [fact['id']], 'message': '%s: %s!%s has no usable saved value.' % (fact['title'], name, address)})
    return {'source_hash': source_hash, 'facts': facts, 'issues': issues, 'inventory': inventory, 'expected_sheets': expected_sheets}


def extract_document_tables(document):
    """Native table cells are source facts; OCR and prose remain reviewable evidence."""
    manifest = getattr(document, 'extraction_manifest', None) or {}
    text = getattr(document, 'extracted_text', None) or ''
    source_hash = manifest.get('content_sha256') or digest([manifest,text])
    pages = manifest.get('pages') or [{'page':1,'text':text,'tables':[],'provenance':'unstructured','status':'unknown'}]
    facts, issues, inventory = [], [], []
    for page in pages:
        number = page['page']; page_text = str(page.get('text') or '')
        tables = page.get('tables') or []
        if not tables:
            lines = page_text.splitlines()
            groups, pending = [], []
            for line in [*lines,'']:
                if '|' in line or '\t' in line:
                    cells = [v.strip() for v in re.split(r'\||\t', line.strip().strip('|'))]
                    if not all(re.fullmatch(r':?-+:?',v or '-') for v in cells): pending.append(cells)
                else:
                    if len(pending)>1: groups.append(pending)
                    pending=[]
            tables = groups
        inventory.append({'page':number,'tables':len(tables),'status':page.get('status','unknown')})
        if page.get('status') not in {'complete','unknown'}:
            issues.append({'kind':'extraction_incomplete','document_id':str(document.id),'message':'%s page %s has incomplete extraction.' % (document.title,number)})
        for table_index, table in enumerate(tables):
            if len(table)<2: continue
            headers = table[0]
            for row_index,row in enumerate(table[1:],1):
                if not row: continue
                label = str(row[0] or '')
                for column,raw in enumerate(row[1:],1):
                    native = '' if raw is None else str(raw).strip()
                    # Native cells must contain a single monetary/ratio observation.
                    cleaned = re.sub(r'₹|[$€£]|\b(?:INR|USD|EUR|GBP|Mn|Cr|million|crore|thousand|lakh)\b|%', '',native,flags=re.I).strip()
                    amount = decimal(cleaned)
                    if amount is None: continue
                    units = units_from_labels([native, str(headers[0] or ''), page_text[:1000]])
                    period = period_from_headers([str(headers[column] or '')]) if column<len(headers) else period_from_headers([])
                    metric = ALIASES.get(clean(label))
                    if '%' in native: units={'kind':'ratio','currency':None,'scale':'1','label':'%'};metric = metric+'_margin' if metric else None
                    locator={'page':number,'table':table_index,'row':row_index,'column':column}
                    fact={'id':digest([str(document.id),source_hash,locator]),'document_id':str(document.id),'source_hash':source_hash,'title':document.title,
                        'locator':locator,'original':native,'value':format(amount,'f'),'formula':'','number_format':'','label':label,'metric':metric,
                        'period':period,'units':units,'basis':'unknown','entity':'target','scenario':'unspecified','source_series':str(document.id)+':page'+str(number)+':table'+str(table_index),
                        'state':'supported' if page.get('provenance')=='native_text' else 'ocr_unverified','provenance':page.get('provenance','unstructured')}
                    facts.append(fact)
        if not tables and re.search(r'\b(?:revenue|EBITDA|PAT|balance sheet|cash flow|net sales)\b',page_text,re.I):
            issues.append({'kind':'unstructured_source','document_id':str(document.id),'message':'%s page %s contains financial prose without a source-located table. Its text is retained for review.' % (document.title,number)})
    if manifest.get('truncated') or manifest.get('pending_pages'):
        issues.append({'kind':'extraction_incomplete','document_id':str(document.id),'message':'%s has pending or unreadable source pages.' % document.title})
    return {'source_hash':source_hash,'facts':facts,'issues':issues,'inventory':inventory,'expected_sheets':[]}


def fact_key(fact):
    return (fact['metric'], fact['period']['key'], fact['period']['status'], fact['basis'], fact['entity'], fact['scenario'], fact['units']['currency'], fact['units']['kind'], fact.get('source_series', '') if fact['period']['status'] == 'unknown' or not fact['units']['currency'] or not fact['units']['scale'] else '')


def comparable(fact):
    return fact.get('metric') and fact['state'] == 'supported' and fact['period']['scope'] != 'unknown' and fact['units']['kind'] == 'amount'


def resolve_facts(facts):
    groups = defaultdict(list)
    for fact in facts:
        if comparable(fact): groups[fact_key(fact)].append(fact)
    cells = []; issues = []
    for key, candidates in sorted(groups.items(), key=lambda pair: str(pair[0])):
        retained = candidates
        if key[0]=='revenue':
            priority=min(f.get('revenue_priority',1) for f in candidates)
            candidates=[f for f in candidates if f.get('revenue_priority',1)==priority]
            if len(candidates)!=len(retained):
                issues.append({'kind':'revenue_classification','fact_ids':[f['id'] for f in retained],'message':'%s uses source operating/net revenue; other total-revenue classifications remain in the source inventory.' % key[1]})
        amounts = defaultdict(list)
        for fact in candidates:
            source_amount = Decimal(fact['value']) * Decimal(fact['units']['scale'] or '1')
            if fact['metric'] in {'cogs','expenses','depreciation'}: source_amount = abs(source_amount)
            amounts[source_amount].append(fact)
        selected = candidates[0]
        state = 'supported' if len(amounts) == 1 else 'conflicted'
        if state == 'conflicted': issues.append({'kind': 'conflict', 'fact_ids': [f['id'] for f in candidates], 'message': '%s %s has conflicting comparable source values.' % (key[0], key[1])})
        cell = {'metric': key[0], 'period': selected['period'], 'basis': key[3], 'entity': key[4], 'scenario': key[5], 'currency': key[6],
                'value': format(next(iter(amounts)), 'f') if state == 'supported' else None, 'state': state, 'native_units': not selected['units']['currency'] or not selected['units']['scale'], 'unit_labels':selected['units'],
                'source_series': selected.get('source_series', key[8]), 'fact_ids': [f['id'] for f in retained], 'selected_fact_ids':[f['id'] for f in candidates], 'alternatives': [{'value': format(value, 'f'), 'fact_ids': [f['id'] for f in members]} for value, members in amounts.items()]}
        cells.append(cell)
    # Flow metrics may be summed; stock balances and ratios may not.
    flows = set(PNL_KEYS) | {'operating_cash_flow', 'investing_cash_flow', 'financing_cash_flow', 'capex'}
    periods = defaultdict(list)
    for cell in cells:
        if cell['metric'] in flows and cell['period']['scope'] == 'quarterly':
            periods[(cell['metric'], cell['period']['year'], cell['period']['status'], cell['basis'], cell['entity'], cell['scenario'], cell['currency'], cell.get('source_series', ''), cell.get('native_units',False))].append(cell)
    for key, inputs in periods.items():
        indexes = {c['period']['index'] for c in inputs}
        if indexes != {1, 2, 3, 4} or len(inputs) != 4 or any(c['state'] != 'supported' for c in inputs): continue
        annual = next((c for c in cells if (c['metric'], c['period']['year'], c['period']['status'], c['basis'], c['entity'], c['scenario'], c['currency'], c.get('source_series', ''), c.get('native_units',False)) == key and c['period']['scope'] == 'annual'), None)
        value = sum(Decimal(c['value']) for c in inputs)
        ids = list(dict.fromkeys(f for c in inputs for f in c['fact_ids']))
        if annual:
            if annual['state'] == 'supported' and abs(Decimal(annual['value']) - value) > Decimal('0.005'):
                annual['state'] = 'conflicted'; annual['value'] = None; annual['fact_ids'] = list(dict.fromkeys(annual['fact_ids']+ids))
                annual['alternatives'].append({'value': format(value, 'f'), 'fact_ids': ids})
                issues.append({'kind': 'aggregation_conflict', 'fact_ids': annual['fact_ids'], 'message': '%s FY%02d annual value disagrees with its complete quarter total.' % (key[0], key[1])})
            continue
        p = {**inputs[0]['period'], 'scope': 'annual', 'index': None, 'key': 'FY%02d' % key[1], 'label': 'FY%02d' % key[1]}
        cells.append({**inputs[0], 'period': p, 'value': format(value, 'f'), 'state': 'derived', 'fact_ids': ids,
                      'selected_fact_ids':ids, 'calculation': {'operation': 'sum_quarters', 'inputs': [c['period']['key'] for c in sorted(inputs, key=lambda c: c['period']['index'])]}, 'alternatives': []})
    series = defaultdict(dict)
    for cell in cells:
        if cell['state'] in {'supported', 'derived'}:
            series[(cell['period']['key'], cell['period']['status'], cell['basis'], cell['entity'], cell['scenario'], cell['currency'], cell.get('source_series', ''))][cell['metric']] = cell
    for values in series.values():
        for output, left, right in [('ebit', 'ebitda', 'depreciation'), ('expenses', 'gross_profit', 'ebitda'), ('pat', 'profit_before_tax', 'tax')]:
            if output in values or left not in values or right not in values: continue
            a,b = values[left], values[right]
            if a['period']['scope'] != b['period']['scope']: continue
            amount = Decimal(a['value']) - Decimal(b['value'])
            if output == 'expenses' and amount < 0:
                issues.append({'kind': 'bridge_conflict', 'message':'Gross profit less EBITDA produces a negative expense; classification needs review.', 'fact_ids':a['fact_ids']+b['fact_ids']})
                continue
            derived = {**a, 'metric':output, 'value':format(amount, 'f'), 'state':'derived',
                'fact_ids':list(dict.fromkeys(a['fact_ids']+b['fact_ids'])), 'selected_fact_ids':list(dict.fromkeys(a.get('selected_fact_ids',a['fact_ids'])+b.get('selected_fact_ids',b['fact_ids']))), 'alternatives':[],
                'calculation':{'operation':'subtract','left':left,'right':right,'input_fact_ids':[a['fact_ids'],b['fact_ids']]}}
            cells.append(derived);values[output] = derived
    return cells, issues


def build_snapshot(documents, reviews=None):
    facts, issues, runs = [], [], []
    for doc in documents:
        manifest = getattr(doc, 'extraction_manifest', None) or {}
        if manifest.get('sheets'):
            run = extract_manifest(doc); facts.extend(run['facts']); issues.extend(run['issues']); runs.append({'document_id': str(doc.id), **{k:v for k,v in run.items() if k != 'facts'}})
        else:
            run = extract_document_tables(doc); facts.extend(run['facts']); issues.extend(run['issues'])
            runs.append({'document_id':str(doc.id), **{k:v for k,v in run.items() if k != 'facts'}})
    for fact in facts:
        review = (reviews or {}).get(fact['id'])
        if review:
            fact.update({key:value for key,value in review['mapping'].items() if key in {'metric','period','units','basis','entity','scenario'}})
            fact['analyst_review'] = {key:review[key] for key in ['id','actor_id','reason']}
    cells, conflicts = resolve_facts(facts); issues.extend(conflicts)
    mapped = {f['id'] for f in facts if f['metric']}
    accounted = {fid for cell in cells for fid in cell['fact_ids']}
    for fact in facts:
        if fact['id'] in mapped and fact['id'] not in accounted:
            issues.append({'kind': 'unresolved_fact', 'fact_ids': [fact['id']], 'message': '%s: %s %s needs period, units, or value verification.' % (fact['title'], fact['label'], fact['locator'].get('cell', 'page %s' % fact['locator'].get('page')))})
    alternatives = defaultdict(list)
    for cell in cells:
        if cell['metric'] in PNL_KEYS and cell['period']['scope'] == 'annual' and cell['entity'] == 'target' and cell['state'] in {'supported','derived'}:
            alternatives[(cell['metric'],cell['period']['key'],cell['period']['status'],cell['currency'],cell['basis'])].append(cell)
    for key, variants in alternatives.items():
        if len(variants) > 1 and max(Decimal(c['value']) for c in variants) - min(Decimal(c['value']) for c in variants) > Decimal('5000'):
            issues.append({'kind':'source_series_difference','fact_ids':list(dict.fromkeys(f for c in variants for f in c['fact_ids'])),
                'message':'%s %s has differing source schedules in %s: %s. Verify reporting status, scope and vintage.' % (key[0],key[1],key[3],', '.join(format(Decimal(c['value'])/Decimal('1000000'), '.2f')+' Mn' for c in variants))})
    for cell in cells:
        if cell.get('native_units'):
            issues.append({'kind':'units_unknown','fact_ids':cell['fact_ids'],'message':'%s %s is retained in native source units; currency or scale needs verification.' % (cell['metric'],cell['period']['key'])})
        if cell['metric'] in PNL_KEYS and cell['period']['scope']=='annual' and cell['entity']=='target' and (cell['period']['status']=='unknown' or cell['basis']=='unknown'):
            issues.append({'kind':'reporting_basis_unknown','fact_ids':cell['fact_ids'],'message':'%s %s needs explicit actual/forecast or standalone/consolidated confirmation.' % (cell['metric'],cell['period']['key'])})
    coverage = {'facts_retained': len(facts), 'mapped_facts': len(mapped), 'accounted_facts': len(accounted), 'unresolved_mapped_facts': len(mapped-accounted),
                'unmapped_facts': len(facts)-len(mapped), 'source_count': len(runs), 'issue_count': len(issues),
                'status': 'incomplete' if any(i['kind'] != 'conflict' for i in issues) else 'complete_with_conflicts' if conflicts else 'complete'}
    snapshot = {'contract': CONTRACT, 'source_fingerprint': digest([(r['document_id'], r['source_hash']) for r in runs]), 'facts': facts, 'cells': cells, 'issues': issues, 'coverage': coverage, 'runs': runs}
    snapshot['fingerprint'] = digest(snapshot)
    return snapshot


def persist_snapshot(deal, snapshot):
    """Immutable snapshots; a per-deal lock prevents mixed concurrent inventories."""
    from django.db import transaction
    from deals.models import Deal, FinancialExtractionRun, FinancialFact, FinancialSnapshot
    with transaction.atomic():
        Deal.objects.select_for_update().get(pk=deal.pk)
        for run in snapshot['runs']:
            record, _ = FinancialExtractionRun.objects.get_or_create(deal=deal, document_id=run['document_id'], source_hash=run['source_hash'], contract=CONTRACT,
                defaults={'inventory': run['inventory'], 'issues': run['issues'], 'state': 'incomplete' if run['issues'] else 'complete'})
            FinancialFact.objects.bulk_create([FinancialFact(id=f['id'], run=record, payload=f) for f in snapshot['facts'] if f['document_id'] == run['document_id']], ignore_conflicts=True, batch_size=500)
        payload = {k:v for k,v in snapshot.items() if k != 'facts'}
        payload['reviewed_facts'] = {f['id']: f for f in snapshot['facts'] if f.get('analyst_review')}
        record, _ = FinancialSnapshot.objects.get_or_create(deal=deal, fingerprint=snapshot['fingerprint'], defaults={'source_fingerprint': snapshot['source_fingerprint'], 'payload': payload})
        return str(record.id)


def snapshot_for_deal(deal, *, persist=True):
    documents = list(deal.documents.all().order_by('id'))
    reviews = {}
    for review in deal.financial_fact_reviews.order_by('created_at'):
        reviews[review.fact_id] = {'id':str(review.id), 'actor_id':review.actor_id, 'mapping':review.mapping, 'reason':review.reason}
    snapshot = build_snapshot(documents, reviews=reviews)
    if persist: snapshot['id'] = persist_snapshot(deal, snapshot)
    return snapshot


def verified_cell_value(cell, source_facts):
    """Recompute snapshot amounts from immutable primary inputs, not cited summaries."""
    if cell.get('state') not in {'supported','derived'}: return None
    facts = [f for f in source_facts if f['id'] in cell.get('selected_fact_ids', cell.get('fact_ids', []))]
    if not facts or any(not comparable(f) or f['units']['currency'] != cell['currency'] for f in facts): return None
    op = (cell.get('calculation') or {}).get('operation')
    values = [abs(Decimal(f['value'])*Decimal(f['units']['scale'] or '1')) if f['metric'] in {'cogs','expenses','depreciation'} else Decimal(f['value'])*Decimal(f['units']['scale'] or '1') for f in facts]
    if not op:
        return values[0] if all(abs(v-values[0]) <= Decimal('.0001') for v in values) else None
    if op == 'sum_quarters':
        by_quarter = defaultdict(set)
        for fact,value in zip(facts,values):
            if fact['period']['scope'] != 'quarterly' or fact['period']['year'] != cell['period']['year']: return None
            by_quarter[fact['period']['index']].add(value)
        if set(by_quarter) != {1,2,3,4} or any(len(v)!=1 for v in by_quarter.values()): return None
        return sum(next(iter(v)) for v in by_quarter.values())
    if op == 'subtract':
        calculation = cell['calculation']; operands=[]
        for metric in [calculation['left'],calculation['right']]:
            candidates = [f for f in facts if f['metric']==metric]
            if not candidates: return None
            amounts = {Decimal(f['value'])*Decimal(f['units']['scale'] or '1') for f in candidates if f['period']['scope']==cell['period']['scope']}
            if len(amounts)==1: operands.append(next(iter(amounts)))
            else:
                quarters=defaultdict(set)
                for fact in candidates:
                    if fact['period']['scope']!='quarterly': return None
                    quarters[fact['period']['index']].add(Decimal(fact['value'])*Decimal(fact['units']['scale'] or '1'))
                if set(quarters)!={1,2,3,4} or any(len(v)!=1 for v in quarters.values()): return None
                operands.append(sum(next(iter(v)) for v in quarters.values()))
        return operands[0]-operands[1]
    return None


def render_main_table(snapshot, citations):
    """Render one comparable P&L view, retaining other views in snapshot cells."""
    from ai_orchestrator.services.report_financial_format import FINANCIAL_ROWS
    groups = defaultdict(list)
    for cell in snapshot['cells']:
        if cell['metric'] in PNL_KEYS and cell['period']['scope'] in {'annual', 'half_year', 'ytd', 'ltm'}:
            groups[(cell['basis'], cell['entity'], cell['scenario'], cell['currency'], cell.get('source_series', ''), cell.get('native_units',False))].append(cell)
    if not groups: return None, []
    key = sorted(groups, key=lambda k: (k[1] != 'target', not bool(re.search(r'P&L|profit.?loss|income.?statement|overall.?PL|\bPL\b',k[4],re.I)), -sum(c['state'] in {'supported', 'derived'} for c in groups[k]), -sum(c['metric'] == 'revenue' and c['state'] in {'supported', 'derived'} for c in groups[k]), str(k)))[0]
    cells = groups[key]
    periods = sorted({(c['period']['key'], c['period']['status']) for c in cells}, key=lambda p: (p[0], p[1]))
    lookup = {(c['metric'], c['period']['key'], c['period']['status']): c for c in cells}
    facts = {f['id']: f for f in snapshot['facts']}
    labels = [p + ' ' + s.title() for p,s in periods]
    header = 'Metric (native model units; currency/scale Not provided)' if key[5] else 'Metric (%s Mn; %s; %s)' % (key[3],key[0],key[1])
    rows = ['| %s | %s |' % (header, ' | '.join(labels)), '| --- | '+' | '.join('---' for _ in periods)+' |']
    unresolved = []
    for metric, label in zip(PNL_KEYS, FINANCIAL_ROWS):
        values = []
        for p, status in periods:
            cell = lookup.get((metric, p, status))
            if not cell or cell['state'] not in {'supported', 'derived'}:
                values.append('Not provided'); unresolved.append({'metric': metric, 'period': p, 'status': cell['state'] if cell else 'absent'}); continue
            rank = max([int(str(n).lstrip('Rr')) for n in citations] or [0]) + 1
            source = facts[cell.get('selected_fact_ids',cell['fact_ids'])[0]]
            verified = verified_cell_value(cell, [facts[fid] for fid in cell['fact_ids']])
            if verified is None or abs(verified-Decimal(cell['value'])) > Decimal('.0001'):
                values.append('Not provided'); unresolved.append({'metric':metric,'period':p,'status':'calculation_unverified'}); continue
            amount = verified / Decimal(source['units']['scale'] or '1') if key[5] else verified / Decimal('1000000')
            citations[str(rank)] = {'document_id': source['document_id'], 'title': source['title'], 'location': '%s!%s' % (source['locator']['sheet'], source['locator']['cell']) if source['locator'].get('sheet') else 'page %s table %s row %s column %s' % (source['locator']['page'],source['locator']['table'],source['locator']['row'],source['locator']['column']),
                'snapshot_cell': cell, 'snapshot_facts': [facts[fid] for fid in cell['fact_ids']], 'financial_cells': {},
                'reference': '%s, %s:%s; %s%s' % (source['title'], source['locator'].get('sheet','page'), source['locator'].get('cell',source['locator'].get('page')), p, '; complete quarter aggregation' if cell['state'] == 'derived' else ''),
                'url': next((c.get('url') for c in citations.values() if c.get('document_id') == source['document_id'] and c.get('url')), '')}
            values.append(format(amount.quantize(Decimal('.01'), rounding=ROUND_HALF_UP), '.2f') + ' [R%03d]' % rank)
        rows.append('| %s | %s |' % (label, ' | '.join(values)))
    return '\n'.join(rows), unresolved


def apply_snapshot_table(text, snapshot, citations, prepared=None):
    table, gaps = prepared or render_main_table(snapshot, citations)
    if not table: return text, [{'kind': 'financial_coverage', 'message': 'No comparable source-supported P&L series is available. All retained facts require review.'}]
    from .financial_table_rewrite import locate_main_financial_table
    try:
        _, start, end = locate_main_financial_table(text)
        text = text[:start] + table + text[end:]
    except ValueError:
        text += '\n\n### Main P&L Statement\n\n' + table
    explanation = '\n\nFinancial statement basis: this table is rendered from the saved financial snapshot. Other reporting bases and scenarios remain in the source inventory; absent or conflicted cells are not assumed to be zero.\n'
    text += explanation
    warnings = [{'kind': 'financial_coverage', 'message': 'Financial coverage is %s. %s issues remain; source facts are retained for review.' % (snapshot['coverage']['status'], snapshot['coverage']['issue_count']), 'snapshot_id': snapshot.get('id'), 'details': [i['message'] for i in snapshot['issues']]}] if snapshot['issues'] else []
    if gaps: warnings.append({'kind': 'financial_cells', 'message': '%s main statement cells are absent or unresolved.' % len(gaps), 'details': gaps})
    return text, warnings


def load_snapshot(snapshot_id):
    from deals.models import FinancialSnapshot, FinancialFact
    record = FinancialSnapshot.objects.get(pk=snapshot_id)
    payload = dict(record.payload)
    source_versions = {r['document_id']: r['source_hash'] for r in payload['runs']}
    facts = FinancialFact.objects.filter(run__deal_id=record.deal_id, run__contract=CONTRACT).select_related('run').order_by('id')
    payload['facts'] = [payload.get('reviewed_facts', {}).get(f.id, f.payload) for f in facts if source_versions.get(str(f.run.document_id)) == f.run.source_hash]
    payload['id'] = str(record.id)
    return payload


def financial_coverage_for_deal(deal):
    record = deal.financial_snapshots.first()
    if not record: return {'status': 'unknown', 'snapshot_id': None}
    return {**record.payload['coverage'], 'snapshot_id': str(record.id), 'generated_at': record.created_at.isoformat(),
            'issues': record.payload['issues'], 'source_fingerprint': record.source_fingerprint}


def project_snapshot(deal, snapshot):
    """Legacy profile projection uses only supported annual target cells."""
    from deals.models import VentureIntelligenceCompanyProfile, VentureIntelligenceCompanyRelation, VentureIntelligenceFinancialStatement
    from django.db import transaction
    with transaction.atomic():
        relation = VentureIntelligenceCompanyRelation.objects.filter(deal=deal, relation_type='target').select_related('company_profile').first()
        if not relation:
            profile = VentureIntelligenceCompanyProfile.objects.create(name=deal.title, data_source='internal_documents', company_type='private')
            relation = VentureIntelligenceCompanyRelation.objects.create(deal=deal, company_profile=profile, relation_type='target')
        profile = relation.company_profile
        groups = defaultdict(list)
        for cell in snapshot['cells']:
            if cell['period']['scope'] != 'annual' or cell['entity'] != 'target' or cell['scenario'] != 'unspecified': continue
            # The legacy schema cannot represent budget or unknown actual/forecast status.
            if cell.get('native_units') or cell['period']['status'] not in {'actual', 'forecast'} or cell['basis'] not in {'standalone', 'consolidated'}: continue
            metric = cell['metric']; statement = 'profit_loss' if metric in PNL_KEYS else 'cash_flow' if 'cash_flow' in metric or metric == 'capex' else 'balance_sheet'
            groups[(statement, cell['period']['key'] + ('E' if cell['period']['status'] == 'forecast' else ''), cell['basis'].title())].append(cell)
        for (kind, fy, basis), cells in groups.items():
            row, _ = VentureIntelligenceFinancialStatement.objects.select_for_update().get_or_create(company_profile=profile, statement_type=kind, fy=fy, fin_type=basis, defaults={'data': {}, 'provenance': {}, 'data_source': 'local_ai'})
            data = dict(row.data); provenance = dict(row.provenance); metrics = dict(provenance.get('metrics') or {})
            for cell in cells:
                key = cell['metric']; prior = metrics.get(key, {})
                if key in data and prior.get('source') not in {'local_ai', 'financial_snapshot'} and row.data_source != 'local_ai': continue
                metrics[key] = {'source': 'financial_snapshot', 'extraction_contract': 'primary-financial-fields-v2', 'snapshot_id': snapshot.get('id'), 'status': cell['state'], 'fact_ids': cell['fact_ids']}
                if cell['state'] in {'supported', 'derived'}: data[key] = cell['value'] + ' ' + cell['currency']
                elif prior.get('source') == 'financial_snapshot': data.pop(key, None)
            row.data = data; row.provenance = {**provenance, 'metrics': metrics}; row.save(update_fields=['data', 'provenance'])
    return {'source': CONTRACT, 'snapshot_id': snapshot.get('id'), 'coverage': snapshot['coverage']}


def validate_report(report, snapshot):
    """Financial edits may publish, but removed/unsupported amounts stay explicit."""
    from .financial_table_rewrite import locate_main_financial_table
    from ai_orchestrator.services.report_financial_format import displayed_amount
    issues = []
    try: table, _, _ = locate_main_financial_table(report)
    except ValueError:
        return {'status': 'unverified', 'issues': [{'kind': 'table_missing', 'message': 'The main financial table is missing or ambiguous.'}]}
    rows = [[c.strip() for c in line.strip().strip('|').split('|')] for line in table.splitlines()]
    units = units_from_labels([rows[0][0]])
    for metric, row in zip(PNL_KEYS, rows[2:]):
        for column, content in enumerate(row[1:], 1):
            if column >= len(rows[0]): continue
            period = period_from_headers([rows[0][column]])
            candidates = [c for c in snapshot['cells'] if c['metric'] == metric and c['period']['key'] == period['key'] and c['period']['status'] == period['status'] and c['state'] in {'supported','derived'} and c['currency'] == units['currency']]
            amount = displayed_amount(content)
            if amount is None:
                if candidates: issues.append({'kind': 'omitted_supported_value', 'metric': metric, 'period': period['key'], 'message': '%s %s is missing from the table despite retained source values.' % (metric,period['key'])})
            elif not units['scale'] or not any(abs(Decimal(c['value'])/Decimal(units['scale'])-amount[0]) <= Decimal('.005') for c in candidates):
                issues.append({'kind': 'unverified_edit', 'metric': metric, 'period': period['key'], 'message': '%s %s does not match a compatible source-supported amount.' % (metric,period['key'])})
    return {'status': 'unverified' if issues else 'supported', 'issues': issues, 'snapshot_id': snapshot.get('id')}


def record_report_revision(deal, analysis, before, after, actor=None):
    from deals.models import FinancialReportRevision
    if before == after: return {}
    record = deal.financial_snapshots.first()
    validation = validate_report(after, load_snapshot(record.id)) if record else {'status':'unknown','issues':[{'kind':'legacy_coverage','message':'Financial coverage has not been audited.'}]}
    FinancialReportRevision.objects.create(deal=deal, analysis=analysis, actor=actor, before_hash=hashlib.sha256(before.encode()).hexdigest(),
        after_hash=hashlib.sha256(after.encode()).hexdigest(), before_report=before, after_report=after, snapshot=record, validation=validation)
    return validation


def financial_source_gaps(deal):
    """Expose financial discrepancies through the existing source-gap contract."""
    record = deal.financial_snapshots.first()
    if not record: return []
    grouped = defaultdict(list)
    for issue in record.payload.get('issues', []):
        kind = issue['kind']
        grouped[(kind, issue.get('document_id', ''))].append(issue['message'])
    revision = deal.financial_report_revisions.order_by('-created_at').first()
    if revision:
        current = ((deal.latest_analysis.analysis_json or {}).get('analyst_report') if deal.latest_analysis else deal.deal_summary) or ''
        if hashlib.sha256(current.encode()).hexdigest() == revision.after_hash:
            for issue in revision.validation.get('issues', []): grouped[(issue['kind'], '')].append(issue['message'])
    return [{'source_type':'report_validation', 'source_id': '%s:financial:%s:%s' % (record.id,kind,doc),
             'title':'Key Financials', 'error': '%s financial source %s require review.' % (len(messages), kind.replace('_',' ')),
             'status':'needs_review', 'issue_kind': 'discrepancy' if kind in {'conflict','aggregation_conflict','unverified_edit','source_series_difference'} else 'verification',
             'details': messages, **({'document_id':doc} if doc else {})} for (kind,doc),messages in grouped.items()]
