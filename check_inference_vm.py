import argparse
import json
import sys
from typing import Any

import requests


def _ok(label: str, detail: str = "") -> None:
    suffix = f" {detail}" if detail else ""
    print(f"[OK] {label}{suffix}")


def _fail(label: str, detail: str = "") -> None:
    suffix = f" {detail}" if detail else ""
    print(f"[FAIL] {label}{suffix}")


def _join_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _response_json(response: requests.Response) -> Any:
    try:
        return response.json()
    except Exception:
        return None


def _short_body(response: requests.Response, limit: int = 1200) -> str:
    try:
        text = response.text
    except Exception:
        text = ""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _render_payload(payload: Any, limit: int = 1200) -> str:
    try:
        text = json.dumps(payload, ensure_ascii=True)
    except Exception:
        text = repr(payload)
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def check_vllm(base_url: str, api_key: str, model: str, timeout: int) -> bool:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    try:
        models_resp = requests.get(_join_url(base_url, "/models"), headers=headers, timeout=timeout)
        models_resp.raise_for_status()
        payload = _response_json(models_resp) or {}
        model_ids = [item.get("id") for item in payload.get("data", []) if isinstance(item, dict)]
        if model and model not in model_ids:
            _fail("vLLM models endpoint", f"model '{model}' not found; available={model_ids}")
            return False
        _ok("vLLM models endpoint", f"models={model_ids}")
    except Exception as exc:
        detail = f"error={exc}"
        response = getattr(exc, "response", None)
        if response is not None:
            detail += f" status={response.status_code} body={_short_body(response)!r}"
        _fail("vLLM models endpoint", detail)
        return False

    if not model:
        _ok("vLLM chat probe", "skipped because no model was provided")
        return True

    body = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with the single word: healthy"}],
        "temperature": 0,
        "max_tokens": 8,
    }
    try:
        chat_resp = requests.post(
            _join_url(base_url, "/chat/completions"),
            headers={**headers, "Content-Type": "application/json"},
            json=body,
            timeout=timeout,
        )
        chat_resp.raise_for_status()
        payload = _response_json(chat_resp) or {}
        choices = payload.get("choices") or []
        content = ""
        if choices and isinstance(choices[0], dict):
            message = choices[0].get("message") or {}
            content = str(message.get("content") or "").strip()
        _ok("vLLM chat probe", f"response={content!r}")
        return True
    except Exception as exc:
        detail = f"error={exc}"
        response = getattr(exc, "response", None)
        if response is not None:
            detail += f" status={response.status_code} body={_short_body(response)!r}"
        _fail("vLLM chat probe", detail)
        return False


def check_embedding_service(base_url: str, model: str, timeout: int) -> bool:
    health_url = base_url.rstrip("/")
    if health_url.endswith("/v1"):
        health_url = health_url[:-3]
    try:
        health_resp = requests.get(_join_url(health_url, "/health"), timeout=timeout)
        health_resp.raise_for_status()
        body = health_resp.text.strip()
        _ok("Embedding service health", f"status_code={health_resp.status_code} body={body!r}")
    except Exception as exc:
        detail = f"error={exc}"
        response = getattr(exc, "response", None)
        if response is not None:
            detail += f" status={response.status_code} body={_short_body(response)!r}"
        _fail("Embedding service health", detail)
        return False

    body = {
        "model": model,
        "input": "health check embedding probe",
    }
    try:
        embed_resp = requests.post(
            _join_url(base_url, "/embeddings"),
            headers={"Content-Type": "application/json"},
            json=body,
            timeout=timeout,
        )
        embed_resp.raise_for_status()
        payload = _response_json(embed_resp) or {}
        data = payload.get("data") or []
        embedding = ((data[0] if data else {}) or {}).get("embedding") or []
        dims = len(embedding)
        if dims <= 0:
            _fail("Embedding inference", f"received an empty embedding vector payload={_render_payload(payload)!r}")
            return False
        _ok("Embedding inference", f"dims={dims} payload_sample={_render_payload({'model': payload.get('model'), 'usage': payload.get('usage')})!r}")
        return True
    except Exception as exc:
        detail = f"error={exc}"
        response = getattr(exc, "response", None)
        if response is not None:
            detail += f" status={response.status_code} body={_short_body(response)!r}"
        _fail("Embedding inference", detail)
        return False


def check_reranker(base_url: str, model: str, timeout: int) -> bool:
    body = {
        "model": model,
        "query": "Which line is about revenue?",
        "texts": [
            "Revenue grew strongly year over year.",
            "The company moved offices in April.",
        ],
        "documents": [
            "Revenue grew strongly year over year.",
            "The company moved offices in April.",
        ],
    }
    try:
        rerank_resp = requests.post(
            _join_url(base_url, "/rerank"),
            headers={"Content-Type": "application/json"},
            json=body,
            timeout=timeout,
        )
        rerank_resp.raise_for_status()
        payload = _response_json(rerank_resp) or {}
        if isinstance(payload, list):
            results = payload
        elif isinstance(payload, dict):
            results = payload.get("results") or payload.get("data") or payload.get("rerank") or payload
        else:
            results = payload
        if not results:
            _fail("Reranker inference", f"received no ranking results payload={_render_payload(payload)!r}")
            return False
        top = results[0] if isinstance(results, list) else results
        _ok("Reranker inference", f"top={_render_payload(top)!r} raw_payload={_render_payload(payload)!r}")
        return True
    except Exception as exc:
        detail = f"error={exc}"
        response = getattr(exc, "response", None)
        if response is not None:
            detail += f" status={response.status_code} body={_short_body(response)!r}"
        _fail("Reranker inference", detail)
        return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check inference VM health for vLLM, embeddings, and reranker.")
    parser.add_argument("--vm-ip", default="127.0.0.1", help="VM IP or hostname. Default: 127.0.0.1")
    parser.add_argument("--vllm-base-url", default="", help="Full vLLM base URL. Example: http://1.2.3.4:8000/v1")
    parser.add_argument("--embedding-base-url", default="", help="Full embedding base URL. Example: http://1.2.3.4:8081/v1")
    parser.add_argument("--reranker-base-url", default="", help="Full reranker base URL. Example: http://1.2.3.4:8082")
    parser.add_argument("--api-key", default="local-dev-key", help="vLLM API key")
    parser.add_argument("--vllm-model", default="Qwen/Qwen3.6-35B-A3B", help="vLLM model id to probe")
    parser.add_argument("--embedding-model", default="Qwen/Qwen3-Embedding-0.6B", help="Embedding model id to probe")
    parser.add_argument("--reranker-model", default="BAAI/bge-reranker-v2-m3", help="Reranker model id to probe")
    parser.add_argument("--timeout", type=int, default=20, help="HTTP timeout in seconds")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    vm_ip = args.vm_ip
    vllm_base_url = args.vllm_base_url or f"http://{vm_ip}:8000/v1"
    embedding_base_url = args.embedding_base_url or f"http://{vm_ip}:8081/v1"
    reranker_base_url = args.reranker_base_url or f"http://{vm_ip}:8082"

    print("Checking inference services")
    print(f"  vLLM:      {vllm_base_url}")
    print(f"  Embedding: {embedding_base_url}")
    print(f"  Reranker:  {reranker_base_url}")
    print("")

    checks = [
        check_vllm(vllm_base_url, args.api_key, args.vllm_model, args.timeout),
        check_embedding_service(embedding_base_url, args.embedding_model, args.timeout),
        check_reranker(reranker_base_url, args.reranker_model, args.timeout),
    ]

    if all(checks):
        print("\nAll inference services are reachable and responded correctly.")
        return 0

    print("\nOne or more inference checks failed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
