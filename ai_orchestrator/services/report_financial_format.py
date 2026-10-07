"""A single, consistent income statement for the Key Financials section."""
import re
from decimal import Decimal

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


def financial_bridge_errors(rows: list[list[str]]) -> list[str]:
    """Check available displayed amounts, allowing their combined rounding error."""
    def amount(cell):
        value = re.sub(r"\[\d+\]", "", cell).replace(",", "").replace("*", "").strip()
        match = re.fullmatch(r"(\()?([-+]?\d+(?:\.\d+)?)(\))?", value)
        if not match or bool(match[1]) != bool(match[3]):
            return None
        number = Decimal(match[2]) * (-1 if match[1] else 1)
        precision = len(match[2].split(".")[1]) if "." in match[2] else 0
        return number, Decimal("0.5") * Decimal(10) ** -precision

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
            cells = [amount(amounts[name][column]) for name in [result, *[name for name, _ in inputs]]]
            if any(cell is None for cell in cells):
                continue
            actual = cells[0][0]
            expected = sum(cell[0] * sign for cell, (_, sign) in zip(cells[1:], inputs))
            tolerance = sum(cell[1] for cell in cells) + Decimal("0.00001")
            if abs(actual - expected) > tolerance:
                errors.append(f"{result} in {period}: displayed {actual}, bridge yields {expected}")
    return errors
