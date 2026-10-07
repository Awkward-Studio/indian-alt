"""A single, consistent income statement for the Key Financials section."""

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
