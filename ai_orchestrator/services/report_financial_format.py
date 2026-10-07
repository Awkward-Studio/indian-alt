"""A single, consistent income statement for the Key Financials section."""
import re
from decimal import Decimal


def displayed_amount(cell):
    value = re.sub(r"\[\d+\]", "", cell).replace(",", "").replace("*", "").strip()
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
FINANCIAL_TABLE_INSTRUCTION = (
    "\n\nKey Financials table format:\n"
    "- Produce exactly ONE Markdown table in this section: a consolidated P&L / "
    "income statement. Put currency and scale in the first column header and the "
    "available historical and forecast periods across columns, clearly marked Actual "
    "or Forecast. Use exactly these metric rows in this order:\n"
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
    "with paragraphs or bullets below this single table. Show supported formulas and "
    "their cited inputs in prose. Do not create additional evidence-base, financial, "
    "sensitivity or action tables here; route actions to Next Steps. Keep all material "
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


def financial_source_errors(rows: list[list[str]], citations: list[dict]) -> list[str]:
    """Compare direct workbook figures with cited saved values in the same period and units.

    This checks extracted Excel results, not recalculation of arbitrary Excel
    functions. Unknown source periods or units are not claimed as verified.
    """
    def scale(text):
        for pattern, value in [(r"\b(?:crores?|cr)\b", 10_000_000), (r"\blakhs?\b", 100_000),
                               (r"\b(?:millions?|mn)\b", 1_000_000), (r"\b(?:thousands?|000)\b", 1000),
                               (r"\b(?:rupees?|INR|USD)\b", 1)]:
            if re.search(pattern, text, re.I):
                return Decimal(value)
        return None

    def period(text):
        match = re.search(r"\b(FY|CY)?\s*(20\d{2}|\d{2})(?:[AEF])?\b", re.sub(r"\[\d+\]", "", text), re.I)
        return ((match[1] or "FY").upper(), match[2][-2:]) if match else None

    aliases = {
        "Revenue": {"revenue", "revenues", "totalrevenue", "operatingrevenue", "revenuefromoperations", "netsales", "sales"},
        "Cost of Goods Sold": {"costofgoodssold", "cogs", "totalcostofrevenue", "costofrevenue", "costofsales"},
        "Gross Profit": {"grossprofit"},
        "Operating Expenses": {"operatingexpenses", "totaloperatingexpenses", "opex"},
        "EBITDA": {"ebitda", "operatingebitda"},
        "Depreciation and Amortization": {"depreciationandamortization", "depreciationamortization", "depreciation", "da"},
        "EBIT": {"ebit", "operatingprofit"},
        "Net Finance Costs": {"netfinancecosts", "financecosts", "interestexpense", "interestexpenses", "interestcost"},
        "Other Non-operating Income / Expenses": {"otherincome", "othernonoperatingincomeexpenses", "nonoperatingincome"},
        "Exceptional Items": {"exceptionalitems", "exceptionalitem"},
        "PBT": {"pbt", "profitbeforetax", "profitbeforetaxpbt", "profitbeforetaxation"},
        "Income Tax Expense": {"incometaxexpense", "taxexpense", "tax", "taxes", "incometax"},
        "PAT": {"pat", "profitaftertax", "profitaftertaxpat", "profitaftertaxation", "netprofit"},
    }
    by_number = {int(c["citation_number"]): c for c in citations}
    table_scale = scale(rows[0][0])
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
            for number in references:
                citation = by_number.get(int(number), {})
                facts = citation.get("financial_cells") or {}
                location = citation.get("used_location") or citation.get("location") or ""
                exact = re.search(r"!([A-Z]{1,3}\d+)(?::([A-Z]{1,3}\d+))?$", location)
                if exact and (exact[2] is None or exact[2] == exact[1]):
                    facts = {exact[1]: facts[exact[1]]} if exact[1] in facts else {}
                for address, fact in facts.items():
                    label = re.sub(r"[^a-z0-9]", "", fact.get("row_label", "").casefold())
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
                    source_scale = scale(" ".join(fact.get("unit_labels") or []))
                    if source_scale is not None:
                        value = Decimal(str(fact["value"])) * source_scale / table_scale
                        if metric in {"Cost of Goods Sold", "Operating Expenses", "Depreciation and Amortization", "Income Tax Expense"}:
                            value = abs(value)
                        candidates.append((value, address))
            if candidates and all(abs(amount[0] - value) > amount[1] + Decimal("0.0000001") for value, _ in candidates):
                values = ", ".join(f"{address}={value}" for value, address in candidates[:4])
                errors.append(f"{metric} in {rows[0][column]}: displayed {amount[0]}, cited saved workbook values {values}")
            elif not candidates and len(references) == 1 and wrong_precise_rows:
                errors.append(f"{metric} in {rows[0][column]} cites a different source metric: {wrong_precise_rows[0]}")
            elif not candidates and len(references) == 1 and wrong_precise_periods:
                errors.append(f"{metric} in {rows[0][column]} cites a different source period: {wrong_precise_periods[0]}")
    return errors
