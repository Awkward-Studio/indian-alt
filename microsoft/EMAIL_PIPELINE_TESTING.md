# Email pipeline testing

The default suite is synthetic and does not contact Microsoft Graph, Celery, or the T4 VM:

```bash
venv/bin/python manage.py test microsoft.tests_email_pipeline microsoft.tests --keepdb
```

It covers Graph pagination and persistence, chronological conversation unfolding, reply-only deltas, HTML quotes, forwards, duplicate bodies, attachments, worker payloads, and failure reporting.

## Live T4 validation

Point Django at the existing OpenAI-compatible T4 endpoint and run the live corpus:

```bash
VLLM_BASE_URL=http://T4_VM_IP:8000/v1 \
VLLM_TEXT_MODEL=gemma-4-12b-it-q8 \
venv/bin/python manage.py test_email_pipeline_t4 \
  --report-json /tmp/email-pipeline-report.json
```

The command checks endpoint health and model discovery, then runs synthetic single-message, nested-reply, HTML-reply, forward, duplicate, and long-body cases through deterministic unfolding plus real `document_normalization`. It exits non-zero if material semantic facts are lost or quoted history leaks into a reply delta. Opaque fixture markers are reported as diagnostics only because normalization models may legitimately remove them.

Use repeated `--case NAME` options to run a subset and `--fail-fast` to stop at the first failure. Available names are `single`, `nested_reply`, `html_reply`, `forward`, `duplicate`, and `long_body`.

The JSON report contains only synthetic identifiers, a credential-free endpoint name, model information, timings, marker assertions, and sanitized errors. The command does not create or modify `Email` records; AI audit records may be created by the normal inference facade.

Deterministically unfolded body deltas bypass model cleanup so their text cannot be truncated or rewritten before extraction. The legacy fallback for callers without a derived delta uses non-overlapping 12,000-character chunks, 768 output tokens, and 120-second requests. Normalization is limited to 2,048 output tokens and 180 seconds. On the next run, interrupted synthetic audits older than 15 minutes are closed as failed so they cannot remain indefinitely in `PROCESSING`.
