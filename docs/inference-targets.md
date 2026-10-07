# Switching hosted inference between H100 and T4

Set one environment variable:

```dotenv
AI_INFERENCE_TARGET=h100
```

Use `t4` to switch back. Restart the backend and every Celery worker after changing
the value. On Railway, edit the variable on **both** `indian-alt` and
`indian-alt-worker`, then redeploy both services. Railway uses service variables
instead of reading your workstation's `.env`.

For a single command from this repository, with Railway CLI authenticated:

```bash
python3 scripts/switch_inference_target.py t4
python3 scripts/switch_inference_target.py h100
```

Let active report jobs finish before switching. The command updates both service
selectors and submits redeployments. Wait for both deployments to become healthy
before regenerating reports. It does not start or stop either GPU VM.

The selected profile uses `H100_` or `T4_` prefixes for model endpoints, API keys,
model names, transport, and VM identity. See `env.example`. The hosted profiles
are already populated, including private H100 forwarding credentials. Text and
planner models follow the selected profile even if a personality has an older
model override. Published prompts remain in the database and are unchanged.

H100 uses Qwen/Qwen3.8-27B through vLLM's OpenAI API. T4 uses
gemma-4-12b-it-q8 with llama.cpp slot monitoring. The application inference queue
remains enabled for both. Both use Qwen/Qwen3-Embedding-0.6B with 1,024 dimensions
and BAAI/bge-reranker-base, so switching does not require reindexing documents.

The H100 profile starts four Celery processes and allows four inference leases.
Report sections run in dependency phases: financials and transaction first,
company/promoter/industry/multiples next, then risk/rationale/exit, and finally
executive summary/next steps. Completed sections supply canonical values for
reconciliation. The T4 profile retains one process and one inference lease.
`H100_CELERY_CONCURRENCY`, `H100_AI_INFERENCE_MAX_CONCURRENT_REQUESTS`, and
`H100_VDR_DURABLE_REPORT_CONCURRENCY` control these limits.

H100 document extraction uses its private docproc service on port 8100 and
GLM-OCR on port 8003 for visual pages. Excel extraction preserves cell addresses,
formulas, saved results and defined names. Report retrieval traces cross-sheet
precedents and gives each retrieved cell a verifiable citation. It does not
recalculate Excel: missing saved results, external workbooks, dynamic references
and dependency limits are reported as gaps.

After deploying the report quality code, preview the published prompt upgrade
with `python manage.py upgrade_ic_report_quality`, then apply it with `--apply`.
The command creates new revisions while preserving each section's business
instructions. It strengthens citation, depth and section ownership rules.
Key Financials uses one Revenue-to-PAT table with a fixed 13-row layout;
balance-sheet, cash-flow and sensitivity analysis remain in prose. Source lists
do not count toward minimum analysis length.

| Budget | T4 | H100 |
| --- | ---: | ---: |
| Model context window | 65,536 | 131,072 |
| Retrieved evidence per report section | 36,000 | 81,920 |
| Total report section input | 40,960 | 90,112 |
| Report section output allowance | 16,384 | 16,384 |
| Retrieval candidates / selected chunk cap | 320 / 240 | 800 / 600 |
| New document evidence segment source | 10,000 | 49,152 |
| New document evidence segment input | 14,336 | 57,344 |
| Spreadsheet segment source | 6,000 | 24,576 |

These are token ceilings, not guaranteed amounts sent with every request. Reports
retrieve more existing indexed chunks together; stored chunk sizes do not change.
The final serialized-request guard reserves room for output and an additional
4,096 tokens. The H100 report input limit also leaves 20,480 tokens for serialization
overhead beyond that reserve and its 16,384-token output allowance. New source
segment sizes apply to future ingestion, not already cached document artifacts.

H100 endpoints listen on loopback on the VM. Each hosted service starts a
reconnecting SSH tunnel, using a pinned host key and a restricted forwarding key.
Keep `INFERENCE_SSH_PRIVATE_KEY` and model API keys in secret variables.
H100 power controls are disabled because the existing Azure identity lacks its
start, deallocate, and Run Command permissions. The VM must be running before
using its profile. T4 retains its existing power-control configuration.

Leaving `AI_INFERENCE_TARGET` blank retains the original unprefixed environment
configuration. Unknown targets and incomplete profiles fail startup rather than
silently sending requests to another VM.
