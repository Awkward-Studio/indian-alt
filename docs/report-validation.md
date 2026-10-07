# Report validation and repair

H100 VDR reports use the published section prompts, primary document chunks and deterministic validation. Automatic model-based source review is removed from generation. The business questions remain based on the October 5 revisions. Published formatting changes add short paragraphs, selective Markdown emphasis, gap and contradiction flags, source citation rules and financial accuracy instructions.

## Acceptance

A section must pass table structure and arithmetic checks. Citations are normalized for display, but missing or unresolved markers, filename labels, unverified links and absent citation support no longer cause rejection. Where matching saved workbook values are available for the cited metric, period and scale, known numerical mismatches still fail. Missing inputs must be stated as missing. The extractor supplies statement rows, period headers, units, formulas and bounded precedent traces from every indexed workbook. Saved Excel results are not independently recalculated.

Key Financials requires one main standardized Revenue-to-PAT table with the thirteen specified rows. Supplemental tables are allowed. Validation locates the main statement by its rows rather than rejecting a section merely for having another table. Missing or duplicate main statements still fail. Numeric amounts and available arithmetic bridges remain checked without requiring row citations.

Calculated rows can inherit their input rows' verified citations only when every displayed calculation reconciles. Cosmetic annotations such as `(Calculated)` and accepted metric aliases are normalized without changing numbers. Native model units can be retained explicitly when currency or scale is not supplied, rather than guessing a conversion.

H100 generation disables internal thinking output and retains a bounded decimal calculator. The calculator accepts numeric arithmetic expressions with +, -, *, /, ** and parentheses. It does not evaluate Python code, access files or fetch data. Up to three request batches of at most 24 calculations are followed by a final report response. Calculator requests/results are recorded in the generation audit. Source authenticity remains a separate check.

The writer organizes each required analytical theme against primary evidence, distinguishes facts from claims, projections and assumptions, uses the calculator for supported derivations, and checks its own output before returning Markdown. There is no second model-based reviewer, review verification or quote-repair call in the generation pipeline. Historical review findings remain available as correction context for Regenerate and AI Rewrite and must be reassessed against fresh evidence.

Word count is optional editorial guidance and never causes rejection or retry. Empty outputs, required statement structure, known source-value mismatches and arithmetic remain acceptance checks. Rejected drafts receive the prior draft and concrete validation feedback after that draft. Legacy length feedback explicitly states that no minimum is enforced. Correction instructions are included when reserving evidence context. Retrying does not promote old drafts or review allegations to primary facts.

Cost classification explanations attached to the standard COGS and Operating Expenses labels move to cited prose while the canonical labels remain in the table. Adjusted or exclusion-based variants do not receive this cosmetic normalization. Explicit verified source citations in period headers can be repeated in uncited numeric cells of the main statement; existing cell citations remain intact. This repeats a declared source and does not independently verify its values. The same source-value and arithmetic checks still apply.

Explicit numeric formulas accept mathematical `^` exponent notation. IRR/CAGR checks evaluate the full power expression rather than a trailing exponent fragment. Incorrect full results still fail.

Numeric forecast ranges in the main statement expand into lower- and upper-bound columns without selecting a midpoint. Existing single values repeat across those bound columns, and reporting qualifiers move into cited notes. Both bounds undergo normal arithmetic and source checks. These columns represent stated bounds, not independently constructed coherent scenarios. Unparseable numeric amounts and inconsistent calculations still fail.

Deterministic validation checks the numerical and citation rules it implements. It is not an independent verification of every qualitative claim. Unavailable primary evidence must be identified as a gap.

Native extractor manifests can store saved numeric values as strings. The citation builder accepts finite numeric strings without changing their precision, and preceding numeric strings are not treated as metric labels. Explicit `₹` labels identify rupees. An unlabelled statement may inherit an explicit, unscaled workbook currency only when no mixed currencies or scaled schedules are present. It never inherits fiscal columns from another worksheet.

Primary saved cell facts can include exact application-computed currency display conversions. Only recognized statement amounts with a single explicit currency and scale receive them; missing or mixed units, quantity rows and percentage formats do not. Writers reuse these results and reserve calculator requests for material derivations not already supplied. Final citation, saved-value and arithmetic checks remain active.

## Evidence integrity and inference

Report evidence bypasses the legacy 180,000-character clipping and arbitrary token truncation. The provider enforces the model context window explicitly. Old section caches are invalidated with the v12 cache key. Primary saved cell facts include their own worksheet's fiscal periods, units and number formats. The writer must not transfer column-year mappings between worksheets.

Arithmetic checks account for the precision of displayed operands and results. For example, 1.73 / 8.94 may be reported as 19.3%, and 40.53 / 20.32 as 2.00x. A materially inconsistent result still fails. Report sections share the configured concurrency, now three on H100, and each free worker receives the next section without waiting for the others. Key Financials and Transaction Details retain scheduling priority.

Report capacity is shared across deals. The coordinator counts processing and retrying sections across active reports and fills free capacity from the oldest eligible report, including the next queued deal when an earlier deal has no sections ready to start. The shared limit is the smaller of report concurrency and inference concurrency, capped at four. Document indexing continues to yield at document boundaries. Each section retains its own delivery owner and generation.

H100 report requests use the configured output budget, currently 16,384 tokens. JSON requests always disable thinking, including callers that explicitly enabled it. Calculator continuations and retries after deterministic validation can add inference work. Automatic source-review requests are not scheduled.

Retrieval budgets include JSON string escaping used by the provider's complete-request check. The H100 source pack reserves room for section instructions, deal data, prior sections and calculator continuations, independently of worker concurrency. No selected block is truncated to make the final request fit. The deployed model context is 131,072 tokens, with an 81,920-token report evidence budget and a 90,112-token report input budget. Post-index deal enrichment uses up to 144,000 characters and 60,000 tokens on this profile.

vLLM queue diagnostics and cleanup do not call llama.cpp `/slots` when `AI_SLOT_TRANSPORT_ENABLED` is false. The frontend counts live inference audits when native slots are unavailable. Historical rejected attempts that were retried are shown separately from accepted completions.

## Verification

The focused backend suite covers calculator precision and expression restrictions, provider continuation and audit traces, source-review packets and coverage, financial source values/years/scales/signs, derived-row citations, cosmetic labels, report cache behavior, retry drafts, queue history and vLLM diagnostics. The combined report, enrichment and ledger regression suite passed 168 tests before the final report-summary isolation check. Frontend TypeScript, targeted ESLint and AI history tests passed.

The v12 generator change passed 81 focused regression tests. These include assertions that generation does not call the source-review utility and still rejects incorrect arithmetic. Live attempts identify `generation_mode=grounded_single_pass` and `source_review_enabled=false` in audit metadata. These markers describe the generation path; they do not certify the report's claims.

A live H100 calculator check requested `100*((303/90)**(1/5)-1)`, executed the application calculator and returned 27.48% after using its decimal result. This verifies the calculator integration, not the underlying investment assumptions.

The hosted five-deal acceptance batch is intended to cover all eleven sections for Shipdelight, ShowroomB2B, Posidex, Aavishkar and Rosier. Rosier has a disclosed unprocessed-image gap, while its financial model is indexed. Final batch results must be recorded separately; passing code tests does not establish report accuracy.

The first v9 acceptance batch was cancelled after the reviewer exhausted its entire output allowance on reasoning without returning JSON. Generation also exhausted its output budget. The v10 changes disable that reasoning output, preserve lossless content through the shared prompt builder, retain truncated responses in audits, and prevent report completion from replacing the short ledger description. Live acceptance results remain separate from code-test results.

Citation validation is disabled in generation audit metadata (`citation_validation_enabled=false`). Calculator verification operates on supplied inputs and does not certify their document lineage. Metrics with exact source-cell locations resolve to primary saved cells only when their advertised values match those cells. Financial-profile saving skips values with no supporting references and does not block report completion when nothing can be synced.
