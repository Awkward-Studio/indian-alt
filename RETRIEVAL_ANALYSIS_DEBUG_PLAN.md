# Retrieval + Analysis Debug Plan

## Summary
Because `VLLM_TEXT_MODEL` and `VLLM_PLANNER_MODEL` are already the same model in this deployment, no extra model-routing change is required to make analysis use the same model as planning. The work should focus on extending the retrieval inspection flow so it can run the real answer-generation step after deal and chunk selection and print the most useful debugging information.

## Key Changes
- Extend `UniversalChatService` with a side-effect-free debug/simulation helper that:
  - builds the planner output
  - selects deals
  - selects chunks
  - assembles final `context_data`
  - runs the real answer-generation prompt against the current shared text/planner model
  - returns one payload containing:
    - normalized planner output
    - selected deals
    - selected chunks
    - retrieval diagnostics
    - final answer
    - compact analysis metadata
- Do not introduce new model-selection logic:
  - rely on the existing runtime configuration because `VLLM_TEXT_MODEL == VLLM_PLANNER_MODEL`
  - optionally record both values in debug metadata so it is obvious at runtime that analysis and planning are using the same model
- Extend `inspect_universal_chat_query` with:
  - `--run-analysis` to execute the final answer step after retrieval
  - `--show-analysis-prompt` to optionally print the rendered answer prompt
  - output that includes:
    - planner
    - named entities
    - selected deals with IDs, titles, retrieval scores
    - selected chunks with:
      - deal title
      - deal ID
      - chunk/document/source name
      - source type
      - source ID
      - chunk index
      - score
    - resolved deal scope used for chunk retrieval
    - final analysis answer
- Add a compact `Analysis Input Summary` section:
  - selected deal count
  - selected deal titles and IDs
  - selected chunk count
  - selected chunk source names
  - source-to-deal mapping for the selected chunks
  - context character count
- Add analysis fields to the simulation/debug JSON payload:
  - `analysis_answer`
  - `analysis_model_used`
  - `analysis_input_summary`
  - `analysis_context_preview`
  - `analysis_prompt_preview` when requested
- Keep output focused on debugging:
  - default console output should show only the highest-signal information
  - full prompt/context dumps should remain opt-in via flags
  - keep existing retrieval diagnostics and add analysis-facing summaries rather than duplicate large dumps

## Public Interfaces
- Extend `inspect_universal_chat_query` with:
  - `--run-analysis`
  - `--show-analysis-prompt`
- Extend the service simulation interface, preferably as:
  - `simulate_query(..., run_analysis: bool = False, include_analysis_prompt: bool = False)`
- Extend the returned debug payload with:
  - `analysis_answer`
  - `analysis_model_used`
  - `analysis_input_summary`
  - `analysis_context_preview`
  - `analysis_prompt_preview` when requested

## Test Plan
- Service tests:
  - `run_analysis=True` returns the final answer plus selected deals/chunks
  - analysis input uses normalized `query_plan` and assembled `context_data`
  - simulation remains side-effect free
- Script/output tests:
  - existing script behavior is unchanged without `--run-analysis`
  - `--run-analysis` prints planner, deals, chunks, and final answer
  - `--json` includes the new analysis fields
  - `--show-analysis-prompt` includes the rendered answer prompt
- Debugging scenarios:
  - single-deal query shows one selected deal and its chunks plus the answer
  - named comparison shows both deals and only their chunks plus the comparison answer
  - stats query can show zero chunks but still produce an answer from stats/context
  - document-focused query shows report chunk names and an answer grounded in those chunks

## Assumptions
- `VLLM_TEXT_MODEL` and `VLLM_PLANNER_MODEL` remain the same model in this deployment.
- No separate production model override is required for this feature because the current answer path already resolves to the same model you want.
- The most important debugging information is:
  - planner output
  - selected deals
  - selected chunk provenance
  - analysis context summary
  - final answer text
