# T4 recovery and validation

The September 9 crash reports CUDA allocation failure with llama-server using
13,704 MiB and the embedding services using another 2,184 MiB. Check live usage
before treating the deployment as recovered.

The T4 Compose defaults disable the multimodal projector, offload all model
layers to GPU, and use batch/ubatch sizes of 256/128. The context is 65,536
tokens. Automatic fitting is disabled, so it cannot reduce the context or move
layers to CPU. The Q8_0 model and both GPU TEI services are retained.
Validate this configuration under simultaneous embedding and text workloads.

Existing deployment environment values override Compose defaults. Set these in
the inference deployment environment before recreating the text container:

```dotenv
LLAMA_CONTEXT_SIZE=65536
LLAMA_GPU_LAYERS=all
LLAMA_BATCH_SIZE=256
LLAMA_UBATCH_SIZE=128
LLAMA_PARALLEL_SEQUENCES=1
```

Keep `ALLOW_SHARED_MODEL_DOCUMENT_VISION=false` on the backend and workers.
Configure `DOC_PROCESSOR_URL` for dedicated OCR. Without it, scans remain failed
or partial; native document text is retained. The OCR service must also be
checked to ensure it does not forward images to this text-only endpoint.

Before rollout, record the current container image digest and environment for
rollback without publishing credentials. Check the deployed llama-server help
for `--no-mmproj`, `--fit off`, and `--n-gpu-layers all`. The Compose
image tag is mutable; pin a tested digest after live validation.

After applying the deployment configuration:

1. Confirm startup logs show no vision projector and report the intended context,
   batches, and layer placement. Check free GPU memory with both TEI services up.
2. Run a text request near the configured context budget while exercising
   embeddings and reranking. Watch GPU peak use and container restart counts.
3. Retry one failed email and one linked folder through the application's normal
   retry actions. Include a native PDF, a mixed PDF, and an image-only document.
4. Verify extraction completeness, artifact completion, and worker errors. Check
   that OCR failure preserves partial evidence without sending images to llama.
5. Resume wider processing only after these checks pass. If CUDA runs out of
   memory, record peak usage and the failing request size before changing the
   configuration. This profile does not fall back to CPU layers.

Do not clear queues, stored evidence, or caches as part of this configuration
change. A container restart alone does not prove pipeline recovery.
