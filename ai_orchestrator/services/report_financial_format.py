"""A single, consistent income statement for the Key Financials section."""
import re
from decimal import Decimal, localcontext

FINANCIAL_BASIS_RULE = (
    "Financial arithmetic and reporting basis: 1 crore = 10,000,000 currency units; "
    "1 lakh = 100,000; 1 million = 1,000,000; 1 thousand = 1,000. "
    "Convert amounts reported in thousands (Rs '000) to crore by dividing by 10,000, "
    "lakhs by 100, millions by 10, and rupees by 10,000,000. "
    "For example, 100,000 in Rs '000 is INR 10.00 Cr, not 1.00 Cr or 100.00 Cr. "
    "A currency conversion needs an explicitly sourced or clearly labeled assumed FX rate. "
    "A funding ask is not enterprise value. Post-money equity = primary investment / "
    "post-money ownership; enterprise value additionally requires a same-period debt/cash "
    "bridge. Do not invent that bridge or treat missing debt/cash as zero. "
    "An illustrative midpoint stake is an assumption, not an agreed term. "
    "For each formula, use matching source periods, currency/scale and reporting basis; "
    "do not mix actual and forecast, adjusted and unadjusted EBITDA, or worksheet years. "
    "Do not replace unknown units with guessed units."
)


def source_unit_conversion_notes(content, kind):
    """Exact arithmetic aid for explicitly scaled primary statement text.

    Keep the source unchanged. This does not assign periods, classify rows or
    reconcile statements, and never infers units from a generated summary.
    """
    if kind not in {'normalized_text', 'document_text'}:
        return ''
    headings = re.findall(r"^\s*\(?((?:Rs\.?|INR|Rupees)\s+(?:in\s+)?(?:['’‘]?000|thousands?|lakhs?|lacs?|millions?|crores?))\)?\s*$",
                          content, re.I | re.M)
    if not headings:
        return ''
    scales = set()
    for heading in headings:
        if re.search(r"000|thousand", heading, re.I): scales.add(Decimal('1000'))
        elif re.search(r"lakh|lac", heading, re.I): scales.add(Decimal('100000'))
        elif re.search(r"million", heading, re.I): scales.add(Decimal('1000000'))
        elif re.search(r"crore", heading, re.I): scales.add(Decimal('10000000'))
    if len(scales) != 1:
        return ''
    multiplier = scales.pop()
    notes = []
    seen = set()
    for line in content.splitlines():
        # Per-share figures can have a different scale from the statement.
        if re.search(r'earnings? per share|earning per share|\bEPS\b', line, re.I):
            break
        for literal in line.strip().strip('|').split('|'):
            literal = literal.strip()
            if not re.fullmatch(r'\(?[-+]?\d[\d,]*\.\d+\)?', literal):
                continue
            if literal in seen:
                continue
            parsed = displayed_amount(literal)
            if parsed is None:
                continue
            seen.add(literal)
            value = parsed[0]
            crore = format(value * multiplier / Decimal('10000000'), 'f')
            million = format(value * multiplier / Decimal('1000000'), 'f')
            notes.append(f'{literal} source units = INR {crore} Cr = INR {million} million')
            if len(notes) >= 48:
                break
        if len(notes) >= 48:
            break
    return ('\nExact unit-conversion arithmetic for the explicit heading '+headings[0]+
            ' (derived arithmetic, not new source facts; retain original row, period and scope):\n'+
            '\n'.join(notes)) if notes else ''


def displayed_amount(cell):
    value = re.sub(r"\[[^]\n]+\]", "", cell).replace(",", "").replace("*", "").strip()
    match = re.fullmatch(r"(\()?([-+]?\d+(?:\.\d+)?)(\))?", value)
    if not match or bool(match[1]) != bool(match[3]):
        return None
    number = Decimal(match[2]) * (-1 if match[1] else 1)
    precision = len(match[2].split(".")[1]) if "." in match[2] else 0
    return number, Decimal("0.5") * Decimal(10) ** -precision

FINANCIAL_ROWS = (
    "Revenue", "Cost of Goods Sold", "Gross Profit", "Operating Expenses",
    "EBITDA", "Depreciation and Amortization", "EBIT", "Net Finance Costs",
    "Other Non-operating Income / Expenses", "Exceptional Items", "PBT",
    "Income Tax Expense", "PAT",
)


def financial_period_key(text: str):
    """Compare fiscal ranges by their ending year, preserving calendar/academic labels."""
    text = re.sub(r'\[[^]\n]+\]', '', str(text))
    date = re.search(r'\b(20\d{2})-\d{2}-\d{2}\b', text)
    if date:
        return 'FY', date[1][-2:]
    match = re.search(r'\b(FY|CY|AY)?\s*(20\d{2}|\d{2})(?:\s*[-/–]\s*(20\d{2}|\d{2}))?\s*[AEFP]?\b', text, re.I)
    return ((match[1] or 'FY').upper(), (match[3] or match[2])[-2:]) if match else None
FINANCIAL_SOURCE_ALIASES = {
    "Revenue": {"revenue", "revenues", "totalrevenue", "operatingrevenue", "revenuefromoperations", "netsales", "sales"},
    "Cost of Goods Sold": {"costofgoodssold", "cogs", "totalcostofrevenue", "costofrevenue", "costofsales"},
    "Gross Profit": {"grossprofit"},
    "Operating Expenses": {"operatingexpenses", "totaloperatingexpenses", "operatingexpenditure", "totaloperatingexpenditure", "opex"},
    "EBITDA": {"ebitda", "operatingebitda"},
    "Depreciation and Amortization": {"depreciationandamortization", "depreciationamortization", "depreciation", "da"},
    "EBIT": {"ebit", "operatingprofit"},
    "Net Finance Costs": {"netfinancecosts", "financecosts", "netinterest", "netinterestexpense", "interestexp", "interestexpense", "interestexpenses", "interestcost"},
    "Other Non-operating Income / Expenses": {"otherincome", "othernonoperatingincomeexpenses", "nonoperatingincome"},
    "Exceptional Items": {"exceptionalitems", "exceptionalitem"},
    "PBT": {"pbt", "profitbeforetax", "profitbeforetaxpbt", "profitbeforetaxation"},
    "Income Tax Expense": {"incometaxexpense", "taxexpense", "tax", "taxes", "incometax"},
    "PAT": {"pat", "profitaftertax", "profitaftertaxpat", "profitaftertaxation", "netprofit", "netincome"},
}


def prepared_display_values(fact: dict) -> dict:
    """Exact conversions of supported statement amounts; no inferred units."""
    label = re.sub(r'[^a-z0-9]', '', str(fact.get('row_label') or '').casefold())
    if not any(label in names for names in FINANCIAL_SOURCE_ALIASES.values()) or '%' in fact.get('number_format', ''):
        return {}
    currencies, scales = set(), set()
    for unit in fact.get('unit_labels') or []:
        for currency, pattern in [('INR', r'₹|\b(?:INR|rupees?)\b'), ('USD', r'\bUSD\b'),
                                  ('EUR', r'\bEUR\b|€'), ('GBP', r'\bGBP\b|£')]:
            if re.search(pattern, unit, re.I): currencies.add(currency)
        matched = False
        for pattern, scale in [(r'\b(?:crores?|cr)\b', 10000000), (r'\b(?:lakhs?|lacs?)\b', 100000),
                               (r'\b(?:millions?|mn|mln)\b', 1000000), (r'\b(?:thousands?|000)\b', 1000)]:
            if re.search(pattern, unit, re.I): scales.add(scale); matched = True
        if not matched and re.search(r'₹|€|£|\b(?:INR|USD|EUR|GBP|rupees?)\b', unit, re.I): scales.add(1)
    if len(currencies) != 1 or len(scales) != 1 or isinstance(fact.get('value'), bool):
        return {}
    try:
        value = Decimal(str(fact['value']))
        if not value.is_finite() or abs(value) > Decimal('1e30'):
            return {}
        with localcontext() as context:
            context.prec = 64
            native = value * next(iter(scales))
            currency = next(iter(currencies))
            return {currency + ' ' + name: {'exact': format(native / scale, 'f'),
                    'display_2dp': format((native / scale).quantize(Decimal('.01')), 'f')}
                    for name, scale in [('Cr', Decimal(10000000)), ('million', Decimal(1000000))]}
    except (ValueError, ArithmeticError, KeyError):
        return {}

FINANCIAL_TABLE_INSTRUCTION = (
    "\n\nKey Financials table format:\n"
    "- Include one main standardized Markdown table in this section: a consolidated P&L / "
    "income statement. Put currency and scale in the first column header and the "
    "available historical and forecast periods across columns, clearly marked Actual "
    "or Forecast. State currency and scale explicitly, such as Metric (INR Cr, amounts) "
    "or Metric (USD million, amounts). Every row in this Revenue-to-PAT table is a "
    "monetary amount; do not insert percentages into an amount cell. "
    "Display numeric amounts to exactly two decimal places, including 0.00. Calculate "
    "using the unrounded saved source values, then round only the displayed results. "
    "Show growth and margin percentages with a % suffix in the analysis below, and multiples with x. "
    "When currency or scale is not stated in the primary source, label the header "
    "Metric (native model units; currency/scale Not provided). Never guess units. "
    "Use exactly these metric rows in this order:\n"
    + "\n".join(f"  {index}. {name}" for index, name in enumerate(FINANCIAL_ROWS, 1))
    + "\n- Revenue must be the first row and PAT the last. Cite every supported row/cell "
    "with its exact evidence marker. State Not provided for missing inputs; never fill "
    "an absent expense, tax or profit with zero. Label expenses as positive deductions "
    "and income/expense adjustments with a clear sign convention. Operating Expenses "
    "exclude depreciation/amortization, finance costs and tax to avoid double counting. "
    "Other income, exceptional items and tax credits can require source-specific signs. "
    "Do not invent COGS or EBITDA if the source lacks a compatible classification. "
    "Explain source differences and supported calculation bridges in prose.\n"
    "- Discuss revenue growth, margins, cash flow, working capital, debt, assets, model "
    "dependencies, balance-sheet reconciliation and sensitivities in ### subsections "
    "with paragraphs, bullets or supplemental tables below the main statement. Show supported formulas and "
    "their cited inputs. Supplemental source comparisons, cash-flow, working-capital and "
    "sensitivity tables are allowed; keep units, periods and citations explicit. Do not "
    "replace or duplicate the main Revenue-to-PAT statement; route actions to Next Steps. Keep all material "
    "analytical themes from the instructions above."
)

FINANCIAL_BRIDGE_RULE = (
    "\nFinancial bridge check: Use operating revenue and operating EBITDA in the "
    "standard table. Reconcile Gross Profit = Revenue - COGS; EBITDA = Gross Profit "
    "- Operating Expenses; EBIT = EBITDA - D&A; PBT = EBIT - Net Finance Costs + "
    "signed Other Non-operating Income / Expenses + signed Exceptional Items; "
    "PAT = PBT - Income Tax Expense. Check every available period using the cited "
    "inputs. If reported EBITDA already includes other income, deduct that income "
    "to show operating EBITDA here and explain the reported-to-operating bridge in "
    "prose. Never add the same income twice. Do not silently change source figures "
    "or invent missing adjustments to force a reconciliation; mark unavailable "
    "compatible metrics Not provided and explain unresolved differences. Align each "
    "growth rate and margin with the correct year, denominator and reporting basis.\n"
)

FINANCIAL_SOURCE_RULE = (
    "\nFinancial source and period check: Before writing any financial value, verify "
    "its source row label, worksheet, column period, units and saved Excel result. "
    "Cite the exact supplied cell/range marker for workbook figures. A cash-flow "
    "Capex row is NOT depreciation or amortization; use the income-statement D&A "
    "row. Never assume that the same column letter represents the same year on "
    "different worksheets. State the arithmetic and unit conversion explicitly "
    "for every calculated figure. Use one consistent reporting basis for each "
    "period's entire income-statement column: do not combine model EBITDA with "
    "audited D&A, other income, finance costs or tax to fabricate a hybrid PAT. "
    "Present source disagreements in prose instead. Missing or uncited forecast "
    "costs, tax and adjustments are Not provided, never zero. If the source has "
    "zero, cite that actual zero cell. Do not assert a line is included in EBITDA "
    "or that a difference is caused by strategic spending without tracing its "
    "source formula. Distinguish saved workbook outputs from calculations you "
    "actually perform and disclose missing, stale or truncated dependencies. "
    "Check narrative figures as carefully as the table, including crore/lakh/"
    "million/thousand conversions and negative values.\n"
    "If the workbook does not declare currency or scale, label its table as native "
    "model units with currency/scale Not provided and preserve the saved values. "
    "Do not silently assign INR, crore or million to an unspecified scale; explain "
    "the unit gap before interpreting absolute amounts. Dimensionless ratios from "
    "compatible native-unit values can still be calculated and cited.\n"
)


def financial_bridge_errors(rows: list[list[str]]) -> list[str]:
    """Check available displayed amounts, allowing their combined rounding error."""
    equations = {
        "Gross Profit": [("Revenue", 1), ("Cost of Goods Sold", -1)],
        "EBITDA": [("Gross Profit", 1), ("Operating Expenses", -1)],
        "EBIT": [("EBITDA", 1), ("Depreciation and Amortization", -1)],
        "PBT": [("EBIT", 1), ("Net Finance Costs", -1), ("Other Non-operating Income / Expenses", 1), ("Exceptional Items", 1)],
        "PAT": [("PBT", 1), ("Income Tax Expense", -1)],
    }
    amounts = dict(zip(FINANCIAL_ROWS, rows[2:]))
    errors = []
    for column, period in enumerate(rows[0][1:], 1):
        for result, inputs in equations.items():
            cells = [displayed_amount(amounts[name][column]) for name in [result, *[name for name, _ in inputs]]]
            if any(cell is None for cell in cells):
                continue
            actual = cells[0][0]
            expected = sum(cell[0] * sign for cell, (_, sign) in zip(cells[1:], inputs))
            tolerance = sum(cell[1] for cell in cells) + Decimal("0.00001")
            if abs(actual - expected) > tolerance:
                errors.append(f"{result} in {period}: displayed {actual}, bridge yields {expected}")
    return errors


def financial_source_errors(rows: list[list[str]], citations: list[dict], *, check_citation_support: bool = True) -> list[str]:
    """Compare direct workbook figures with cited saved values in the same period and units.

    This checks extracted Excel results, not recalculation of arbitrary Excel
    functions. Unknown source periods or units are not claimed as verified.
    """
    def scale(text):
        for pattern, value in [(r"\b(?:crores?|cr)\b", 10_000_000), (r"\b(?:lakhs?|lacs?)\b", 100_000),
                               (r"\b(?:millions?|mn)\b", 1_000_000), (r"\b(?:thousands?|000)\b", 1000),
                               (r"₹|\b(?:rupees?|INR|USD)\b", 1)]:
            if re.search(pattern, text, re.I):
                return Decimal(value)
        return None

    def period(text):
        return financial_period_key(text)

    aliases = FINANCIAL_SOURCE_ALIASES
    by_number = {int(c["citation_number"]): c for c in citations}
    table_scale = scale(rows[0][0])
    native_units = bool(re.search(r"native\s+(?:model\s+)?units",rows[0][0],re.I))
    if native_units: table_scale=Decimal(1)
    if table_scale is None:
        return []
    errors = []
    for metric, row in zip(FINANCIAL_ROWS, rows[2:]):
        for column, cell in enumerate(row[1:], 1):
            amount = displayed_amount(cell)
            if amount is None:
                continue
            references = re.findall(r"\[(\d+)\]", cell) or re.findall(r"\[(\d+)\]", row[0])
            candidates = []
            wrong_precise_rows = []
            wrong_precise_periods = []
            source_ledgers = {}
            workbook_references = False
            for number in references:
                citation = by_number.get(int(number), {})
                workbook_references |= bool(re.search(r"\.(?:xlsx|xlsm|xlsb|xls)$", str(citation.get("title") or ""), re.I))
                facts = citation.get("financial_cells") or {}
                location = citation.get("used_location") or citation.get("location") or ""
                exact = re.search(r"!([A-Z]{1,3}\d+)(?::([A-Z]{1,3}\d+))?$", location)
                if exact and (exact[2] is None or exact[2] == exact[1]):
                    facts = {exact[1]: facts[exact[1]]} if exact[1] in facts else {}
                for address, fact in facts.items():
                    label = re.sub(r"[^a-z0-9]", "", fact.get("row_label", "").casefold())
                    source_scale = next((value for text in fact.get("unit_labels") or []
                        if (value := scale(text)) is not None and value != 1), None)
                    if source_scale is None:
                        source_scale = scale(" ".join(fact.get("unit_labels") or []))
                    if native_units: source_scale=Decimal(1)
                    if source_scale is not None and fact.get("period") and period(fact["period"]) == period(rows[0][column]) and "%" not in fact.get("number_format", ""):
                        source_value = Decimal(str(fact["value"])) * source_scale / table_scale
                        source_metric = next((name for name,names in aliases.items() if label in names),None)
                        if source_metric:
                            key=(citation.get("document_id"), (citation.get("locator") or {}).get("sheet_name"))
                            source_ledgers.setdefault(key,{}).setdefault(source_metric,set()).add(source_value)
                    if label not in aliases[metric]:
                        if exact and (exact[2] is None or exact[2] == exact[1]) and label:
                            wrong_precise_rows.append(f"{address} is labelled {fact['row_label']!r}")
                        continue
                    if period(fact.get("period", "")) != period(rows[0][column]) or not fact.get("period"):
                        if exact and (exact[2] is None or exact[2] == exact[1]) and fact.get("period"):
                            wrong_precise_periods.append(f"{address} belongs to {fact['period']}")
                        continue
                    if "%" in fact.get("number_format", ""):
                        continue
                    if source_scale is not None:
                        value = Decimal(str(fact["value"])) * source_scale / table_scale
                        if metric in {"Cost of Goods Sold", "Operating Expenses", "Depreciation and Amortization"}:
                            value = abs(value)
                        candidates.append((value, address))
            equations={
                'Gross Profit':[('Revenue',1),('Cost of Goods Sold',-1)],
                'Operating Expenses':[('Gross Profit',1),('EBITDA',-1)],
                'EBITDA':[('Gross Profit',1),('Operating Expenses',-1)],
                'EBIT':[('EBITDA',1),('Depreciation and Amortization',-1)],
                'PAT':[('PBT',1),('Income Tax Expense',-1)],
                'Income Tax Expense':[('PBT',1),('PAT',-1)],
            }
            for ledger in source_ledgers.values():
                inputs=equations.get(metric)
                if inputs and all(len(ledger.get(name,set()))==1 for name,_ in inputs):
                    candidates.append((sum(next(iter(ledger[name]))*sign for name,sign in inputs),'cited source-input calculation'))
                if metric=='EBITDA' and len(ledger.get('EBITDA',set()))==1 and len(ledger.get('Other Non-operating Income / Expenses',set()))==1:
                    candidates.append((next(iter(ledger['EBITDA']))-next(iter(ledger['Other Non-operating Income / Expenses'])),'reported-to-operating EBITDA bridge'))
            if candidates and all(abs(amount[0] - value) > amount[1] + Decimal("0.0000001") for value, _ in candidates):
                values = ", ".join(f"{address}={value}" for value, address in candidates[:4])
                errors.append(f"{metric} in {rows[0][column]}: displayed {amount[0]}, cited saved workbook values {values}")
            elif check_citation_support and not candidates and len(references) == 1 and wrong_precise_rows:
                errors.append(f"{metric} in {rows[0][column]} cites a different source metric: {wrong_precise_rows[0]}")
            elif check_citation_support and not candidates and len(references) == 1 and wrong_precise_periods:
                errors.append(f"{metric} in {rows[0][column]} cites a different source period: {wrong_precise_periods[0]}")
            elif check_citation_support and not candidates and workbook_references:
                errors.append(f"{metric} in {rows[0][column]} has no matching cited workbook value or supported source-input calculation. Cite its metric, year and units; otherwise mark Not provided.")
    return errors
