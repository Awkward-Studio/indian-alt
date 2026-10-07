# Ledger enrichment and financials

Deal field enrichment uses `deal_ingestion.field_synthesis` and the dedicated `deal_field_synthesis` skill. It extracts strict JSON for ledger fields and banker relationships. It does not use the eleven IC section prompts. Publish the updated field instructions with `python manage.py upgrade_deal_field_synthesis --apply`; the previous published skill revision is archived for rollback.

The field prompt explains description, transaction details, industry context, industry/sub-sector, location, funding amount/use, supported themes and priorities, and external contacts. Candidate reduction must retain complementary source-supported fields. Missing values remain null. JSON requests disable thinking even if callers enable it. H100 map/merge batches allow 144,000 characters and 60,000 estimated tokens in the 131,072-token window, with adaptive subdivision when the complete request cannot fit.

The ledger description comes from `deal_model_data.deal_summary`. Synthesis refreshes AI-owned values and preserves human-owned fields and themes. Additional external banker contacts are retained and linked. Report completion stores the full report in `DealAnalysis` and does not copy it into the ledger description. The original report and analysis history remain available.

The list API supplies `ledger_financials` from target-company financial statements. The ledger shows:

```text
(FY26)
- Revenue: 459 Cr
YoY Growth: 32%
- Gross Margin: 40.5%
- EBITDA %: 11%
- WC Days: 133
```

These numbers are the requested formatting example. Actual values come from each deal's source records. The snapshot prefers the latest closed actual year and consolidated data when both scopes exist for that year. Explicit estimates and future years retain an E label. Missing values say `Not provided`. Revenue is converted to INR crore only when the source currency and scale are known; the existing VI adapter treats bare monetary values as INR crore. The funding ask stays in Investor / Fund raise.

YoY uses consecutive actual fiscal years from the same target profile and reporting scope. Margins may be derived from same-period reported profit and revenue with known units. WC Days uses explicitly supplied working-capital days for the same period and scope, without substituting receivable days or cash-conversion cycle. Target financial relations and statements are prefetched for ledger pages.

The financial extractor is a separate JSON-only step. It retrieves up to 60,000 tokens on H100, keeps source units, and omits unsupported or conflicting facts. Existing stored financial values are not certified by formatting or successful generation. Source quality and legacy extraction errors still require live verification.
