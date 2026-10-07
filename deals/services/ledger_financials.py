"""One consistent fiscal-period snapshot for the deal ledger."""
import re
from decimal import Decimal, InvalidOperation
from django.utils import timezone


def fiscal_period(value):
    text = str(value or '').strip().upper()
    match = re.fullmatch(r'(?:FY\s*)?(20\d{2}|\d{2})(?:[-/](20\d{2}|\d{2}))?\s*(A|E|F|P|ACTUAL|ACTUALS|ESTIMATE|FORECAST|PROJECTED)?', text)
    if not match:
        return None
    year = int(match[2] or match[1])
    year = year + 2000 if year < 100 else year
    forecast = match[3] in {'E', 'F', 'P', 'ESTIMATE', 'FORECAST', 'PROJECTED'}
    today = timezone.localdate()
    last_closed_year = today.year if today.month >= 4 else today.year - 1
    forecast = forecast or year > last_closed_year
    return year, forecast, f'FY{year % 100:02d}' + ('E' if forecast else '')


def number(value):
    text = str(value if value is not None else '').strip().replace(',', '').replace('−', '-')
    if text.startswith('(') and text.endswith(')'):
        text = '-' + text[1:-1]
    matches = re.findall(r'-?\d+(?:\.\d+)?', text)
    if len(matches) != 1:
        return None
    try:
        result = Decimal(matches[0])
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def crore_amount(value, statement):
    amount = number(value)
    if amount is None:
        return None
    data = statement.data or {}
    unit_text = f"{value} {data.get('currency', '')} {data.get('unit', data.get('units', ''))}".lower()
    if re.search(r'\busd\b|\beur\b|\bgbp\b|[$€£]', unit_text):
        return None
    for pattern, divisor in [(r'\bcrores?\b|\bcr\b', 1), (r'\blakhs?\b|\blacs?\b', 100),
                             (r'\bbillions?\b|\bbn\b', Decimal('.01')), (r'\bmillions?\b|\bmn\b', 10),
                             (r'\bthousands?\b|\b000s?\b', 10000)]:
        if re.search(pattern, unit_text):
            return amount / Decimal(divisor)
    if '₹' in unit_text or re.search(r'\binr\b|\brupees\b|\brs\b', unit_text):
        return amount / Decimal(10000000)
    # The existing VI adapter and financial UI use INR crore for bare amounts.
    provenance = ((statement.provenance or {}).get('metrics') or {}).get('revenue') or {}
    if statement.data_source == 'venture_intelligence' and provenance.get('source') != 'local_ai':
        return amount
    return None


def render(value):
    if value is None:
        return None
    return format(value.quantize(Decimal('.01')), 'f').rstrip('0').rstrip('.')


def snapshot(statements):
    rows = [s for s in statements if s.statement_type == 'profit_loss' and fiscal_period(s.fy)]
    if not rows:
        return None
    # Prefer actual data over forecasts and consolidated over standalone for
    # the same year. Never blend periods or scopes to fill missing metrics.
    rows.sort(key=lambda s: (not fiscal_period(s.fy)[1], fiscal_period(s.fy)[0], s.fin_type == 'Consolidated'), reverse=True)
    latest = rows[0]
    year, forecast, label = fiscal_period(latest.fy)
    data = latest.data or {}

    def metric(keys):
        return next((data[k] for k in keys if data.get(k) is not None and data.get(k) != ''), None)

    revenue = crore_amount(metric(['revenue', 'operating_revenue', 'operating_income', 'operational_income', 'net_sales']), latest)
    growth = number(metric(['revenue_growth', 'yoy_growth', 'sales_growth']))
    prior = next((s for s in rows if fiscal_period(s.fy)[:2] == (year - 1, False) and s.fin_type == latest.fin_type), None)
    if growth is None and not forecast and prior and revenue is not None:
        previous = crore_amount(next((prior.data[k] for k in ['revenue', 'operating_revenue', 'operating_income', 'operational_income', 'net_sales'] if prior.data.get(k) is not None), None), prior)
        if previous is not None and previous > 0:
            growth = (revenue / previous - 1) * 100
    gross_margin = number(metric(['gross_margin', 'gross_profit_margin']))
    ebitda_margin = number(metric(['ebitda_margin', 'ebitda_percent']))
    if revenue is not None and revenue > 0:
        if gross_margin is None:
            gross_profit = crore_amount(metric(['gross_profit']), latest)
            if gross_profit is not None:
                gross_margin = gross_profit / revenue * 100
        if ebitda_margin is None:
            ebitda = crore_amount(metric(['ebitda']), latest)
            if ebitda is not None:
                ebitda_margin = ebitda / revenue * 100
    balance = next((s for s in statements if s.statement_type == 'balance_sheet' and fiscal_period(s.fy) and fiscal_period(s.fy)[:2] == (year, forecast) and s.fin_type == latest.fin_type), None)
    wc = number(metric(['working_capital_days', 'wc_days']))
    if wc is None and balance:
        wc = number((balance.data or {}).get('working_capital_days'))
    return {'fy': label, 'revenue_cr': render(revenue), 'yoy_growth_pct': render(growth),
            'gross_margin_pct': render(gross_margin), 'ebitda_margin_pct': render(ebitda_margin),
            'working_capital_days': render(wc), 'fin_type': latest.fin_type,
            'is_forecast': forecast, 'statement_id': str(latest.id)}


def for_deal(deal):
    relations = getattr(deal, 'ledger_target_relations', None)
    if relations is None:
        relations = deal.vi_relations.filter(relation_type='target').select_related('company_profile').prefetch_related('company_profile__financial_statements')
    for relation in relations:
        result = snapshot(list(relation.company_profile.financial_statements.all()))
        if result:
            return result
    return None
