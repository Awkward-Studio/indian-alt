"""Resolve extracted metrics against primary statement cells, years and units."""
import re
from decimal import Decimal
from deals.services.ledger_financials import fiscal_period, number

CONTRACT = 'primary-financial-fields-v2'
ALIASES = {
    'revenue': {'revenue', 'revenues', 'totalrevenue', 'operatingrevenue', 'netsales', 'revenuefromoperations'},
    'cogs': {'cogs', 'costofgoodssold', 'costofmaterials', 'purchasesofservices'},
    'gross_profit': {'grossprofit', 'grossmargin'},
    'gross_margin': {'grossmargin', 'gm', 'grossprofitmargin'},
    'ebitda_margin': {'ebitda', 'ebitdamargin', 'adjustedebitda'},
    'pat': {'pat', 'netprofit', 'profitaftertax'},
    'pat_margin': {'pat', 'patmargin', 'netprofitmargin'},
    'profit_before_tax': {'pbt', 'profitbeforetax'},
    'tax': {'tax', 'taxexpense', 'incometax', 'incometaxexpense'},
    'interest': {'interest', 'financecosts', 'interestexpense'},
    'expenses': {'expenses', 'totalexpenses', 'operatingexpenses', 'otherexpenses'},
    'revenue_growth': {'yoy', 'yoygrowth', 'revenuegrowth', 'salesgrowth'},
}

def clean(text):
    return re.sub(r'[^a-z0-9]', '', str(text).casefold())

def source_year(text):
    # Workbook headers often store actual Excel dates instead of FY labels.
    match = re.match(r'(20\d{2})-03-31', str(text))
    return int(match[1]) if match else (fiscal_period(text) or (None,))[0]

def reported_unit(text):
    currency = next((code for code in ['USD', 'EUR', 'GBP'] if re.search(r'\b'+code+r'\b', text, re.I)), 'INR')
    if '$' in text: currency = 'USD'
    if re.search(r'\b(?:crores?|cr)\b', text, re.I): return currency+' Cr'
    if re.search(r'\b(?:lakhs?|lacs?)\b', text, re.I): return currency+' lakh'
    if re.search(r'\b(?:millions?|mn)\b', text, re.I): return currency+' Mn'
    if re.search(r'\b(?:thousands?|000)\b', text, re.I): return currency+' thousand'
    if currency != 'INR': return currency
    if re.search(r'\b(?:INR|rupees?|Rs)\b|₹', text, re.I): return 'INR'
    return None

def resolve_metric(key, value, fy, refs, citations, evidence_by_ref):
    """Return a source-native value and verified refs; never guess a scale."""
    amount = number(value)
    year = source_year(fy)
    verified = []
    resolved = []
    for ref in refs:
        source = citations[ref]
        cells = source.get('financial_cells') or {}
        if cells:
            for fact in cells.values():
                if source_year(fact.get('period')) != year:
                    continue
                aliases = ALIASES.get(key, {clean(key)})
                if clean(fact.get('row_label')) not in aliases:
                    continue
                percent = '%' in fact.get('number_format', '')
                if percent != ('%' in str(value)):
                    continue
                raw = Decimal(str(fact['value'])) * (100 if percent else 1)
                if amount is None or abs(raw-amount) > max(Decimal('.005'), abs(raw)*Decimal('0.0000001')):
                    continue
                units = list(dict.fromkeys(filter(None, (reported_unit(label) for label in fact.get('unit_labels', [])))))
                unit = '%' if percent else units[0] if len(units) == 1 else None
                native = format(raw, 'f').rstrip('0').rstrip('.') if '.' in format(raw, 'f') else str(raw)
                resolved.append(native + (unit if percent else ' '+unit if unit else ''))
                verified.append(ref)
        else:
            evidence = evidence_by_ref.get(ref, '')
            # Model-generated summaries can contain guessed units and periods.
            locator = source.get('locator') or {}
            if locator.get('kind') in {'metric', 'table_summary', 'claim'} or re.search(r'"(?:key_highlights|confidence)"\s*:', evidence):
                continue
            unit = reported_unit(evidence)
            if amount is not None and unit and '%' not in str(value):
                resolved.append(str(value).strip() if reported_unit(str(value)) == unit else str(amount)+' '+unit)
            else:
                resolved.append(value)
            verified.append(ref)
    if not verified or len(set(map(str, resolved))) != 1:
        return None, []
    return resolved[0], list(dict.fromkeys(verified))
