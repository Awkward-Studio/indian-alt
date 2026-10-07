# Report validation and repair

H100 VDR reports use the published section prompts, primary document chunks and deterministic validation. Automatic model-based source review is removed from generation. The business questions remain based on the October 5 revisions. Published formatting changes add short paragraphs, selective Markdown emphasis, gap and contradiction flags, source citation rules and financial accuracy instructions.

## Acceptance

A section must pass citation normalization, table structure, arithmetic and source checks. Financial statement amounts are compared with cited saved workbook values for the same metric, period and scale. Unsupported workbook values are rejected; missing inputs must be stated as missing. The extractor supplies statement rows, period headers, units, formulas and bounded precedent traces from every indexed workbook. Saved Excel results are not independently recalculated.

Calculated rows can inherit their input rows' verified citations only when every displayed calculation reconciles. Cosmetic annotations such as `(Calculated)` and accepted metric aliases are normalized without changing numbers. Native model units can be retained explicitly when currency or scale is not supplied, rather than guessing a conversion.

H100 generation disables internal thinking output and retains a bounded decimal calculator. The calculator accepts numeric arithmetic expressions with +, -, *, /, ** and parentheses. It does not evaluate Python code, access files or fetch data. Up to three request batches of at most 24 calculations are followed by a final report response. Calculator requests/results are recorded in the generation audit. Source authenticity remains a separate check.

The writer organizes each required analytical theme against primary evidence, distinguishes facts from claims, projections and assumptions, uses the calculator for supported derivations, and checks its own output before returning Markdown. There is no second model-based reviewer, review verification or quote-repair call in the generation pipeline. Historical review findings remain available as correction context for Regenerate and AI Rewrite and must be reassessed against fresh evidence.

The requested section-specific minimum word count is enforced alongside citation, structure, source-value and arithmetic checks. Rejected drafts receive the prior draft and concrete validation feedback. Retrying does not promote old drafts or review allegations to primary facts.

Deterministic validation checks the numerical and citation rules it implements. It is not an independent verification of every qualitative claim. Unavailable primary evidence must be identified as a gap.

## Evidence integrity and inference

Report evidence bypasses the legacy 180,000-character clipping and arbitrary token truncation. The provider enforces the model context window explicitly. Old section caches are invalidated with the v12 cache key. Primary saved cell facts include their own worksheet's fiscal periods, units and number formats. Reviewers must not transfer column-year mappings between worksheets.

Arithmetic checks account for the precision of displayed operands and results. For example, 1.73 / 8.94 may be reported as 19.3%, and 40.53 / 20.32 as 2.00x. A materially inconsistent result still fails. Four report sections start together, and each free worker receives the next section without waiting for the other three. Key Financials and Transaction Details retain scheduling priority.

H100 report requests use the configured output budget, currently 16,384 tokens. Source review reserves 8,192 tokens for JSON findings. JSON requests always disable thinking, including callers that explicitly enabled it. The calculator and reviewer add bounded inference work; generation can take longer than the previous single-pass workflow.

Retrieval budgets include JSON string escaping used by the provider's complete-request check. The H100 source pack reserves room for section instructions, deal data, prior sections and calculator continuations, independently of worker concurrency. Source review sends the draft, requirements and every selected primary block as plain text rather than nesting saved-cell JSON inside another JSON packet. Reviews retain their exact source ranks in audit metadata. No selected block is truncated to make the final request fit.

vLLM queue diagnostics and cleanup do not call llama.cpp `/slots` when `AI_SLOT_TRANSPORT_ENABLED` is false. The frontend counts live inference audits when native slots are unavailable. Historical rejected attempts that were retried are shown separately from accepted completions.

## Verification

The focused backend suite covers calculator precision and expression restrictions, provider continuation and audit traces, source-review packets and coverage, financial source values/years/scales/signs, derived-row citations, cosmetic labels, report cache behavior, retry drafts, queue history and vLLM diagnostics. The combined report, enrichment and ledger regression suite passed 168 tests before the final report-summary isolation check. Frontend TypeScript, targeted ESLint and AI history tests passed.

A live H100 calculator check requested `100*((303/90)**(1/5)-1)`, executed the application calculator and returned 27.48% after using its decimal result. This verifies the calculator integration, not the underlying investment assumptions.

The hosted five-deal acceptance batch is intended to cover all eleven sections for Shipdelight, ShowroomB2B, Posidex, Aavishkar and Rosier. Rosier has a disclosed unprocessed-image gap, while its financial model is indexed. Final batch results must be recorded separately; passing code tests does not establish report accuracy.

The first v9 acceptance batch was cancelled after the reviewer exhausted its entire output allowance on reasoning without returning JSON. Generation also exhausted its output budget. The v10 changes disable that reasoning output, preserve lossless content through the shared prompt builder, retain truncated responses in audits, and prevent report completion from replacing the short ledger description. Live acceptance results remain separate from code-test results.
