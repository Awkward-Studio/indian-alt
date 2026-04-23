#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import requests


DEFAULT_BASE_DIR = Path(__file__).resolve().parent / "data" / "extractions"
REQUIRED_HEADERS = (
    "## Executive Summary",
    "## Strategic Fit & Market Opportunity",
    "## Operational Due Diligence",
    "## Financial Deep Dive",
    "## Risk Matrix (Top 5 Risks)",
    "## Valuation & Exit Range",
    "## Red Flags & Warning Signs",
    "## Next Steps / Data Requests",
)

THINK_TAGS = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
FENCE_PATTERN = re.compile(r"^```(?:json|markdown|md)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


SYSTEM_PROMPT = """You are a senior private equity investment analyst at India Alternatives.
Rewrite the provided deal synthesis into a clean institutional investment memo.

Rules:
- Use only the supplied deal data and document evidence.
- Do not invent numbers, valuation ranges, customers, risks, or documents.
- If evidence is limited, say exactly what is missing and what should be requested.
- Return ONLY valid JSON with one key: "analyst_report".
- "analyst_report" must be markdown, not plain paragraphs.
- Do not wrap the markdown in code fences.

The markdown must follow this exact section order:
1. ## Executive Summary
   - Start with **Verdict:** Buy / Hold / Pass
   - Then include **Top 3 Reasons**
2. ## Strategic Fit & Market Opportunity
3. ## Operational Due Diligence
4. ## Financial Deep Dive
5. ## Risk Matrix (Top 5 Risks)
6. ## Valuation & Exit Range
7. ## Red Flags & Warning Signs
8. ## Next Steps / Data Requests

Formatting requirements:
- Use bullets and compact markdown tables where useful.
- In Financial Deep Dive, separate historical facts from inferred conclusions.
- In Risk Matrix, list the top 5 risks with why it matters and mitigation / validation ask.
- In Valuation & Exit Range, provide a range only when grounded in supplied evidence.
- Keep the report decision-oriented and readable for IC circulation.
"""


def scrub_text(text: str) -> str:
    if not text:
        return ""
    if "</think>" in text.lower():
        text = re.split(r"</think>", text, flags=re.IGNORECASE)[-1]
    text = THINK_TAGS.sub("", text)
    text = FENCE_PATTERN.sub("", text)
    return text.strip()


def extract_json_object(text: str) -> dict[str, Any]:
    cleaned = scrub_text(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    if start == -1:
        raise ValueError("model response did not contain a JSON object")
    decoder = json.JSONDecoder()
    parsed, _ = decoder.raw_decode(cleaned[start:])
    if not isinstance(parsed, dict):
        raise ValueError("model response JSON was not an object")
    return parsed


def get_vllm_config() -> dict[str, str]:
    base_url = os.getenv("VLLM_BASE_URL", "http://20.244.11.248:8000/v1").rstrip("/")
    if not base_url.endswith("/v1"):
        base_url = f"{base_url}/v1"
    return {
        "url": f"{base_url}/chat/completions",
        "api_key": os.getenv("VLLM_API_KEY", "local-dev-key"),
        "model": os.getenv("VLLM_TEXT_MODEL") or os.getenv("VLLM_MODEL") or "Qwen/Qwen3.6-35B-A3B",
    }


def report_quality(report: str) -> dict[str, Any]:
    report = report or ""
    missing_headers = [header for header in REQUIRED_HEADERS if header not in report]
    return {
        "chars": len(report.strip()),
        "missing_headers": missing_headers,
        "has_markdown_headers": "## " in report,
        "needs_repair": len(report.strip()) < 2500 or bool(missing_headers),
    }


def load_artifact(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def compact_documents(documents: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    compacted = []
    for doc in documents[:limit]:
        if not isinstance(doc, dict):
            continue
        compacted.append({
            "document_name": doc.get("document_name") or doc.get("source_file"),
            "document_type": doc.get("document_type") or doc.get("doc_type"),
            "summary": doc.get("summary") or doc.get("document_summary"),
            "metrics": doc.get("metrics") or doc.get("key_metrics") or [],
            "risks": doc.get("risks") or [],
            "tables_summary": doc.get("tables_summary") or [],
            "claims": doc.get("claims") or [],
            "relationship_signal": doc.get("relationship_signal"),
        })
    return compacted


def build_repair_input(artifact: dict[str, Any], max_documents: int) -> dict[str, Any]:
    portable = artifact.get("portable_deal_data") if isinstance(artifact.get("portable_deal_data"), dict) else {}
    metadata = artifact.get("metadata") if isinstance(artifact.get("metadata"), dict) else {}
    documents = metadata.get("documents_used") if isinstance(metadata.get("documents_used"), list) else []
    return {
        "deal_name": artifact.get("deal_name"),
        "deal_model_data": portable.get("deal_model_data") or {},
        "source_relationships": portable.get("source_relationships") or {},
        "current_analyst_report": portable.get("analyst_report") or "",
        "analysis_metadata": portable.get("metadata") or {},
        "document_evidence": compact_documents(documents, max_documents),
        "document_evidence_count": len(documents),
    }


def rewrite_report(artifact: dict[str, Any], *, max_documents: int, timeout: int) -> tuple[str, str]:
    cfg = get_vllm_config()
    repair_input = build_repair_input(artifact, max_documents=max_documents)
    payload = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": "Rewrite this synthesis into the required markdown analyst_report JSON.\n\n"
                + json.dumps(repair_input, ensure_ascii=False, default=str),
            },
        ],
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
        "chat_template_kwargs": {"enable_thinking": False},
    }
    headers = {"Authorization": f"Bearer {cfg['api_key']}"}
    response = requests.post(cfg["url"], json=payload, headers=headers, timeout=(15, timeout))
    response.raise_for_status()
    message = response.json()["choices"][0]["message"]
    parsed = extract_json_object(message.get("content") or "")
    report = scrub_text(parsed.get("analyst_report") or "")
    if not report:
        raise ValueError("model returned an empty analyst_report")
    return report, message.get("thinking") or ""


def iter_artifacts(base_dir: Path):
    for deal_dir in sorted(base_dir.iterdir()):
        if not deal_dir.is_dir():
            continue
        artifact_path = deal_dir / "DEAL_SYNTHESIS.artifact.json"
        if artifact_path.exists():
            yield deal_dir, artifact_path


def matches_filters(deal_dir: Path, artifact: dict[str, Any], filters: set[str]) -> bool:
    if not filters:
        return True
    portable = artifact.get("portable_deal_data") if isinstance(artifact.get("portable_deal_data"), dict) else {}
    model_data = portable.get("deal_model_data") if isinstance(portable.get("deal_model_data"), dict) else {}
    haystack = " ".join([
        deal_dir.name,
        str(artifact.get("deal_name") or ""),
        str(model_data.get("title") or ""),
    ]).lower()
    return any(value in haystack for value in filters)


def run():
    parser = argparse.ArgumentParser(description="Repair weak DEAL_SYNTHESIS analyst_report markdown.")
    parser.add_argument("--base-dir", default=str(DEFAULT_BASE_DIR))
    parser.add_argument("--deal", action="append", dest="deals", help="Filter by folder/deal title substring.")
    parser.add_argument("--apply", action="store_true", help="Write repaired artifacts. Default is dry-run.")
    parser.add_argument("--force", action="store_true", help="Rewrite even if the report already has all required sections.")
    parser.add_argument("--list-only", action="store_true", help="Only list report quality; do not call the LLM.")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--max-documents", type=int, default=120)
    parser.add_argument("--timeout", type=int, default=1200)
    args = parser.parse_args()

    base_dir = Path(args.base_dir).expanduser().resolve()
    filters = {value.lower().strip() for value in args.deals or [] if value and value.strip()}
    selected = []
    for deal_dir, artifact_path in iter_artifacts(base_dir):
        artifact = load_artifact(artifact_path)
        if matches_filters(deal_dir, artifact, filters):
            selected.append((deal_dir, artifact_path, artifact))

    if args.offset:
        selected = selected[args.offset:]
    if args.limit:
        selected = selected[:args.limit]

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] selected_artifacts={len(selected)} base_dir={base_dir}")
    repaired = 0
    skipped = 0
    failed = 0

    for index, (deal_dir, artifact_path, artifact) in enumerate(selected, 1):
        portable = artifact.get("portable_deal_data") if isinstance(artifact.get("portable_deal_data"), dict) else {}
        model_data = portable.get("deal_model_data") if isinstance(portable.get("deal_model_data"), dict) else {}
        title = model_data.get("title") or artifact.get("deal_name") or deal_dir.name
        quality = report_quality(portable.get("analyst_report") or "")
        status = "WEAK" if quality["needs_repair"] else "OK"
        print(
            f"[{index}/{len(selected)}] {status} {title}: "
            f"chars={quality['chars']} missing_headers={len(quality['missing_headers'])}"
        )

        if args.list_only:
            continue
        if not args.force and not quality["needs_repair"]:
            skipped += 1
            continue

        try:
            report, thinking = rewrite_report(
                artifact,
                max_documents=max(args.max_documents, 1),
                timeout=max(args.timeout, 60),
            )
            new_quality = report_quality(report)
            if new_quality["missing_headers"]:
                raise ValueError(f"repaired report still missing headers: {new_quality['missing_headers']}")

            if args.apply:
                portable["analyst_report"] = report
                metadata = artifact.setdefault("metadata", {})
                metadata["report_repaired_at"] = time.time()
                metadata["report_repair_version"] = "structured-markdown-v1"
                if thinking:
                    metadata["report_repair_thinking"] = thinking
                artifact["portable_deal_data"] = portable
                artifact_path.write_text(json.dumps(artifact, indent=2, ensure_ascii=False), encoding="utf-8")
                (deal_dir / "INVESTMENT_REPORT.md").write_text(report, encoding="utf-8")
            repaired += 1
            print(f"    [REPAIRED] chars={new_quality['chars']}")
        except Exception as exc:
            failed += 1
            print(f"    [ERROR] {exc}")

    print("-" * 72)
    print(f"Complete. repaired={repaired} skipped={skipped} failed={failed} list_only={args.list_only}")


if __name__ == "__main__":
    run()
