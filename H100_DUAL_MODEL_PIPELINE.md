# H100 dual-model ingestion architecture

## Selected models

| Responsibility | Model | Endpoint | Why it is separate |
| --- | --- | --- | --- |
| Full-page OCR and document-to-Markdown | `baidu/Unlimited-OCR` | `:8001/v1` | It has a model-specific prompt, decoder controls, and output cleanup. |
| Text normalization, Phase 2 artifacts, and Phase 3 synthesis | `Qwen/Qwen3.8-27B` | `:8000/v1` | Text work benefits from a larger reasoning model and does not need the OCR vision stack. |

Both vLLM servers remain loaded together on the same H100. This avoids unloading and reloading weights when the CLI moves from extraction to artifact creation.

## End-to-end request flow

```text
selected OneDrive deal folders
           |
           v
Phase 1: native readers first
  DOCX/XLSX/PPTX/MSG text where possible
  PDF/image/rendered fallback pages
           |
           +--> Unlimited-OCR :8001
           |      raw page Markdown
           |
           +--> Qwen3.8-27B :8000
                  faithful text normalization
           |
           v
Phase 1 extraction JSON for every selected deal
           |
           v
Phase 2 on every selected deal --> Qwen3.8-27B --> document artifacts
           |
           v
Phase 3 on every selected deal --> Qwen3.8-27B --> deal synthesis/report
```

The orchestrator remains phase-wide: it completes Phase 1 for all selected deals, then Phase 2 for all selected deals, then Phase 3. The services are resident together, but phases do not overlap. This keeps dependencies deterministic and makes whole-run resume straightforward.

## Why the H100 memory split is conservative

The default Compose profile assigns 72% of GPU memory to Qwen and 15% to OCR. The combined vLLM limit is therefore 87%, retaining headroom for CUDA contexts, graphs, and runtime allocations.

- Qwen is capped at a 32,768-token serving context because the pipeline already chunks extracted documents. Reserving its full 262K context would consume KV cache and reduce batching without improving this workflow.
- Qwen uses FP8 KV cache, at most eight sequences, text-only serving, prefix caching, and chunked prefill.
- OCR uses at most two sequences. Its prefix and multimodal processor caches are disabled because pages are not reused.
- Embedding and reranking services are behind the optional `retrieval` Compose profile. Do not enable that profile while both BF16 models share an 80 GB H100; use another GPU/host or stop OCR first.

These are safe starting values, not universal maxima. Measure real page sizes and token lengths before raising concurrency. If model startup runs out of memory during CUDA graph capture, reduce Qwen utilization or add `--enforce-eager`; do not remove the reserved headroom.

## Unlimited-OCR protocol requirements

Unlimited-OCR is not a generic vision-chat replacement. The document processor implements its official vLLM recipe:

1. Every OCR prompt starts with the literal `<image>` token.
2. Requests set `skip_special_tokens` to `false`.
3. Requests pass `ngram_size=35` and `window_size=128`.
4. The OCR server registers `NGramPerReqLogitsProcessor`.
5. The response cleaner keeps `<|ref|>` label text and removes `<|det|>` coordinate boxes.
6. Temperature is zero and the output allowance is 8,192 tokens per page.

The current Phase 1 implementation sends one rendered page per request, so the single-image `window_size=128` recipe is correct. If Phase 1 later sends several pages in one request, change the window to 1,024 for those requests.

## Qwen protocol requirements

Qwen runs with the `qwen3` reasoning parser, but pipeline calls explicitly disable thinking. Phase 2 and Phase 3 expect machine-readable output; hidden reasoning should not consume their output allowance or leak into JSON content.

The text server also uses `--language-model-only`. Although Qwen3.8-27B includes a vision tower, this pipeline deliberately uses Unlimited-OCR for images, so loading Qwen's visual components would waste memory.

## Configuration

The H100 VM keeps `docker-compose.inference.yml` and `vllm.env` directly in the
login user's home directory. This profile runs only the two model servers.
Docproc runs outside this Compose stack:

```bash
cd ~
docker compose --env-file vllm.env -f docker-compose.inference.yml \
  up -d vllm-text vllm-ocr
```

Set `HF_CACHE_DIR` to a mounted filesystem with at least 80 GB free. The default
is `/mnt/vllm-cache/huggingface`. The two model repositories require far more
space than a typical OS disk, and partial downloads also consume space.

Published services:

| Port | Service | Model |
| --- | --- | --- |
| `8000` | OpenAI-compatible text API | `Qwen/Qwen3.8-27B` |
| `8001` | OpenAI-compatible OCR API | `baidu/Unlimited-OCR` |

The Django/bulk host continues to call the document processor for Phase 1 and the text endpoint for Phases 2–3. Existing extraction JSON, document artifact JSON, deal synthesis JSON, and Markdown report formats are unchanged.

## Resume and failure behavior

Model routing does not weaken resume behavior. The interactive CLI records phase state in `data/extractions/audit/pipeline_cli/run_state.json`; each underlying phase also retains its existing output-aware resume behavior.

- If OCR stops, resume Phase 1. Existing valid extraction JSON is retained.
- If normalization or artifact generation stops, resume Phase 2. Successful artifacts and caches are retained.
- If synthesis stops, resume Phase 3. Existing valid analyses are retained.
- A model server restart does not require restarting the entire pipeline. Restore the failed service, then run `bulk_pipeline_cli.py --resume-run --yes`.

## Operational checks

- Confirm both `/v1/models` endpoints list the exact configured model IDs.
- Run one scanned PDF and verify the raw extraction contains Markdown but no `<|det|>` coordinates.
- Run one text-heavy native document and verify its raw native text is preserved alongside normalized text.
- Run the CLI dry-run before a large batch.
- Watch `nvidia-smi` during startup and the first concurrent OCR window; startup success alone does not prove peak runtime headroom.
- Leave the `retrieval` profile disabled throughout Phases 1–3 on an 80 GB card.

## T4 application inference profile

The T4 is separate from document ingestion. It runs the three services used by
the application, including web-search answer generation:

```bash
cd indian-alt
./bootstrap_inference_t4.sh
```

It creates `.env.inference.t4` from `.env.inference.t4.example`, validates the
configuration, pulls the three service images, and starts Gemma, embedding, and
reranking. The `--no-start` option validates without starting containers.

The T4 has no OCR or docproc service. Its llama.cpp server no longer loads the
multimodal projection. Phase 1 document extraction belongs on the H100 path;
the T4 remains available to Django for chat, planning, search, embeddings, and
reranking.

## Source references

- [Baidu Unlimited-OCR model card](https://huggingface.co/baidu/Unlimited-OCR)
- [Official vLLM Unlimited-OCR recipe](https://recipes.vllm.ai/baidu/Unlimited-OCR)
- [Qwen3.8-27B model card](https://huggingface.co/Qwen/Qwen3.8-27B)
- [Official vLLM Qwen3.8-27B recipe](https://recipes.vllm.ai/Qwen/Qwen3.8-27B)
