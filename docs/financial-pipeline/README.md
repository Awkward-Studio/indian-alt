# Financial source retention and report review

Financial snapshots retain numeric source observations separately from model prompts. Each snapshot records source versions, native values, formulas, reporting dimensions, compatible statement cells, calculation inputs, and unresolved issues. Original facts remain immutable when an analyst adds a mapping review.

The main Revenue-to-PAT statement uses deterministic source values and supported calculations. Supporting tables remain available. The financial profile and ledger suppress conflicted values; unknown currency, scale, fiscal status, or reporting basis stays explicit instead of becoming a verified number.

Source reviews complete with `issues_found` or `no_material_error_found`. Inference, malformed response, and review-verification failures receive `incomplete` with a pipeline classification. Source findings do not fail report generation. All unresolved financial items appear in the existing Source gaps panel. AI History displays normalized report sections and the source-review outcome.

## H100 configuration

| Setting | Tokens / requests |
| --- | --- |
| Serving context | 262144 |
| Complete report input allowance | 229376 |
| Maximum retrieved evidence | 212992 |
| Report output allowance | 16384 |
| Application and worker concurrency | 2 |
| llama.cpp slot transport | Disabled |

Retrieval reserves additional space for the deterministic statement and its source inputs. Context includes input and output; 256K is not an unrestricted 256K prompt. vLLM may queue a long request when GPU cache capacity is occupied. The report reviewer and verification passes use the same configured long-input budget.

## Operations

Preview or persist a frozen source audit:

```sh
python manage.py audit_financial_pipeline --deal DEAL_UUID
python manage.py audit_financial_pipeline --cohort-file frozen-cohort.json --apply
```

A table-only repair creates a new analysis version and marks it for review. It does not establish that the surrounding legacy narrative is accurate. Prefer normal report regeneration for the hosted pilot.

Publish the supporting-table prompt contract and start no more than three reports through the existing durable workflow:

```sh
python manage.py upgrade_ic_report_quality --section 'Key Financials' --apply
python manage.py start_financial_report_pilot --deal DEAL_UUID --preview
python manage.py start_financial_report_pilot --deal DEAL_UUID
```

All sections are saved through normal report assembly and displayed on deal pages. The source evidence and older report versions are retained. Inspect both financial findings and pipeline-review failures before treating a report as verified.

Spreadsheet extraction supports source-hash-checked, resumable private batches; PDFs retain page coverage and retry unresolved pages individually. Legacy manifests without complete source coverage remain review items and may require re-extraction. Saved workbook results are not arbitrary Excel recalculations.

## Validation

The pilot cohort was recovered from the ten deal reports produced on 8 October 2026: Aavishkar Strips, Biofics, Finagg, Posidex, Rosier Foods, Shipdelight, ShowroomB2B, Trefoil Packaging, Uolo, and Zouk. Private source fixtures and exploratory model outputs are retained outside Git. Shared regressions cover native units, annual dates on mixed monthly/annual sheets, missing quarter coverage, revenue classification, tax classification, source conflicts, immutable snapshots, supplementary tables, and vLLM status polling.

Source-backed table completeness is separate from narrative review. A correct table does not establish that growth claims, financial interpretations, or source comparisons in prose are correct.

H100 pipelines share a 229376-token input ceiling, reduced when a larger output allowance needs room. Chat evidence and uploads, field synthesis, financial profiles, email decisions and document extraction use expanded budgets. Report evidence reserves space for instructions and calculator continuations. Every H100 text request is checked against the complete 262144-token model window; oversized requests raise an explicit budget error. T4 retains its smaller profile.
