import json
import logging
import math
import re
from typing import Any, Iterator

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


class OllamaProviderService:
    """
    Legacy transport for Ollama-backed generation endpoints.
    Retained for compatibility while the stack transitions to vLLM.
    """

    def __init__(self):
        self.ollama_url = getattr(settings, "OLLAMA_URL", "http://52.172.249.12:11434")

    def get_available_models(self) -> list[str]:
        try:
            response = requests.get(f"{self.ollama_url}/api/tags", timeout=5)
            response.raise_for_status()
            data = response.json()
            return [model["name"] for model in data.get("models", [])]
        except Exception as exc:
            logger.error("Error fetching Ollama models: %s", exc)
            return []

    def unload_model(self, model: str) -> bool:
        if not model:
            return False

        try:
            response = requests.post(
                f"{self.ollama_url}/api/generate",
                json={
                    "model": model,
                    "prompt": "",
                    "stream": False,
                    "keep_alive": 0,
                },
                timeout=15,
            )
            response.raise_for_status()
            logger.info("Requested Ollama unload for model %s", model)
            return True
        except Exception as exc:
            logger.warning("Failed to unload Ollama model %s before text analysis: %s", model, exc)
            return False

    def execute_stream(self, payload: dict) -> Iterator[str]:
        response = requests.post(f"{self.ollama_url}/api/generate", json=payload, stream=True, timeout=300)
        response.raise_for_status()

        for line in response.iter_lines():
            if line:
                yield line.decode("utf-8")

    def execute_standard(self, payload: dict, timeout: int = 3600) -> dict:
        response = requests.post(f"{self.ollama_url}/api/generate", json=payload, timeout=timeout)
        response.raise_for_status()
        return response.json()


class VLLMProviderService:
    """
    OpenAI-compatible transport for vLLM-backed text, vision, and embedding requests.
    Normalizes responses to the legacy internal shape so the orchestration layer can
    keep using the same response contract.
    """

    def __init__(self):
        self.base_url = getattr(settings, "VLLM_BASE_URL", "http://localhost:8000/v1").rstrip("/")
        self.embedding_url = getattr(settings, "VLLM_EMBEDDING_URL", self.base_url).rstrip("/")
        self.api_key = getattr(settings, "VLLM_API_KEY", "")

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _get_completions_url(self) -> str:
        url = self.base_url
        if not url.endswith("/v1"):
            url = f"{url}/v1"
        return f"{url}/chat/completions"

    def get_available_models(self) -> list[str]:
        try:
            url = self.base_url
            if not url.endswith("/v1"):
                url = f"{url}/v1"
            response = requests.get(f"{url}/models", headers=self._headers(), timeout=5)
            response.raise_for_status()
            data = response.json()
            return [model.get("id") for model in data.get("data", []) if model.get("id")]
        except Exception as exc:
            logger.error("Error fetching vLLM models: %s", exc)
            return []

    def health_check(self) -> bool:
        try:
            url = self.base_url
            if not url.endswith("/v1"):
                url = f"{url}/v1"
            response = requests.get(f"{url}/models", headers=self._headers(), timeout=5)
            response.raise_for_status()
            return True
        except Exception as exc:
            logger.warning("vLLM health check failed for %s: %s", self.base_url, exc)
            return False

    def execute_stream(self, payload: dict) -> Iterator[str]:
        body = self._build_chat_body(payload, stream=True)
        response = requests.post(
            self._get_completions_url(),
            headers=self._headers(),
            json=body,
            stream=True,
            timeout=300,
        )
        response.raise_for_status()

        for raw_line in response.iter_lines(decode_unicode=True):
            if not raw_line or raw_line.startswith(":"):
                continue
            if raw_line.startswith("data: "):
                raw_line = raw_line[6:]
            if raw_line == "[DONE]":
                yield json.dumps({"response": "", "done": True})
                break
            try:
                chunk = json.loads(raw_line)
            except json.JSONDecodeError:
                logger.warning("Skipping malformed vLLM stream chunk: %s", raw_line)
                continue

            choice = (chunk.get("choices") or [{}])[0]
            delta = choice.get("delta") or {}
            text = delta.get("content") or ""
            thinking = delta.get("reasoning_content") or delta.get("reasoning") or ""
            done = choice.get("finish_reason") is not None
            yield json.dumps({"response": text, "thinking": self._flatten_content(thinking), "done": done})

    def execute_standard(self, payload: dict, timeout: int = 3600) -> dict:
        body = self._build_chat_body(payload, stream=False)
        response = requests.post(
            self._get_completions_url(),
            headers=self._headers(),
            json=body,
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = message.get("content") or ""
        thinking = message.get("reasoning_content") or message.get("reasoning") or ""

        return {
            "response": self._flatten_content(content),
            "thinking": self._flatten_content(thinking),
            "usage": data.get("usage") or {},
            "raw": data,
        }

    def embed(self, *, model: str, text: str, timeout: int = 30) -> list[float]:
        url = self.embedding_url
        if not url.endswith("/v1"):
            url = f"{url}/v1"
        response = requests.post(
            f"{url}/embeddings",
            headers=self._headers(),
            json={"model": model, "input": text},
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
        return ((data.get("data") or [{}])[0]).get("embedding") or []

    def _build_chat_body(self, payload: dict, *, stream: bool) -> dict[str, Any]:
        model = payload.get("model")
        system_prompt = payload.get("system") or ""
        user_prompt = payload.get("prompt") or ""
        images = payload.get("images") or []

        messages: list[dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        if images:
            content: list[dict[str, Any]] | str = []
            if user_prompt:
                content.append({"type": "text", "text": user_prompt})
            for image in images:
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": self._coerce_image_url(image)},
                    }
                )
        else:
            content = user_prompt

        messages.append({"role": "user", "content": content})

        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": stream,
            "temperature": ((payload.get("options") or {}).get("temperature", 0.1)),
        }

        response_format = payload.get("response_format")
        if response_format:
            body["response_format"] = response_format

        tools = payload.get("tools")
        if tools:
            body["tools"] = tools
            if payload.get("tool_choice"):
                body["tool_choice"] = payload["tool_choice"]

        max_tokens = (payload.get("options") or {}).get("max_tokens") or payload.get("max_tokens")
        if max_tokens:
            body["max_tokens"] = max_tokens

        return body

    @staticmethod
    def _coerce_image_url(image: str) -> str:
        if image.startswith("data:") or image.startswith("http://") or image.startswith("https://"):
            return image
        return f"data:image/png;base64,{image}"

    @staticmethod
    def _flatten_content(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text" and item.get("text"):
                        parts.append(item["text"])
                    elif item.get("type") == "output_text" and item.get("text"):
                        parts.append(item["text"])
                elif isinstance(item, str):
                    parts.append(item)
            return "\n".join(parts).strip()
        return str(content or "")


class EmbeddingProviderService:
    """Provider for dedicated embedding endpoints."""

    def __init__(self):
        self.base_url = getattr(settings, "EMBEDDING_BASE_URL", "").rstrip("/")
        self.api_key = getattr(settings, "EMBEDDING_API_KEY", "")
        self.timeout = getattr(settings, "EMBEDDING_TIMEOUT", 30)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def embed(self, *, model: str, text: str, timeout: int | None = None) -> list[float]:
        if not self.base_url or not model:
            return []
        response = requests.post(
            f"{self.base_url}/embeddings",
            headers=self._headers(),
            json={"model": model, "input": text},
            timeout=timeout or self.timeout,
        )
        response.raise_for_status()
        data = response.json()
        raw_embedding = ((data.get("data") or [{}])[0]).get("embedding") or []
        cleaned: list[float] = []
        for value in raw_embedding:
            if value is None:
                raise ValueError("Embedding provider returned null values.")
            numeric = float(value)
            if not math.isfinite(numeric):
                raise ValueError("Embedding provider returned non-finite values.")
            cleaned.append(numeric)
        return cleaned


class RerankerProviderService:
    """Provider for dedicated reranker endpoints."""

    def __init__(self):
        self.base_url = getattr(settings, "RERANKER_BASE_URL", "").rstrip("/")
        self.api_key = getattr(settings, "RERANKER_API_KEY", "")
        self.timeout = getattr(settings, "RERANKER_TIMEOUT", 30)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _request_payload(
        self,
        *,
        model: str,
        query: str,
        documents: list[str],
        include_model: bool = True,
        include_documents_alias: bool = True,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "query": query,
            "texts": documents,
        }
        if include_model:
            body["model"] = model
        if include_documents_alias:
            body["documents"] = documents
        return body

    def _post_rerank(self, body: dict[str, Any], *, timeout: int) -> Any:
        response = requests.post(
            f"{self.base_url}/rerank",
            headers=self._headers(),
            json=body,
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()

    def rerank(self, *, model: str, query: str, documents: list[str], timeout: int | None = None) -> list[dict[str, Any]]:
        if not self.base_url or not model or not documents:
            return []
        effective_timeout = timeout or self.timeout
        request_variants = [
            self._request_payload(model=model, query=query, documents=documents, include_model=True, include_documents_alias=False),
            self._request_payload(model=model, query=query, documents=documents, include_model=True, include_documents_alias=True),
            self._request_payload(model=model, query=query, documents=documents, include_model=False, include_documents_alias=False),
        ]

        payload: Any = None
        last_error: Exception | None = None
        for body in request_variants:
            try:
                payload = self._post_rerank(body, timeout=effective_timeout)
                break
            except requests.HTTPError as exc:
                last_error = exc
                response = exc.response
                status = response.status_code if response is not None else "unknown"
                text = response.text[:500] if response is not None else ""
                if status == 422 and response is not None:
                    match = re.search(r"maximum allowed batch size (\d+)", response.text)
                    if match:
                        max_batch_size = max(int(match.group(1)), 1)
                        logger.warning(
                            "Reranker endpoint enforces max batch size=%s; chunking %s documents.",
                            max_batch_size,
                            len(documents),
                        )
                        combined: list[dict[str, Any]] = []
                        for start in range(0, len(documents), max_batch_size):
                            chunk_docs = documents[start:start + max_batch_size]
                            chunk_payload = self._request_payload(
                                model=model,
                                query=query,
                                documents=chunk_docs,
                                include_model=True,
                                include_documents_alias=False,
                            )
                            chunk_response = self._post_rerank(chunk_payload, timeout=effective_timeout)
                            chunk_results = chunk_response if isinstance(chunk_response, list) else (
                                chunk_response.get("results") or chunk_response.get("data") or []
                            )
                            for item in chunk_results:
                                if not isinstance(item, dict):
                                    continue
                                index = item.get("index")
                                if index is None:
                                    continue
                                adjusted = dict(item)
                                adjusted["index"] = int(index) + start
                                combined.append(adjusted)
                        payload = combined
                        break
                logger.warning(
                    "Reranker request variant failed status=%s keys=%s body=%s",
                    status,
                    list(body.keys()),
                    text,
                )
            except Exception as exc:
                last_error = exc
                logger.warning("Reranker request variant failed keys=%s error=%s", list(body.keys()), exc)

        if payload is None:
            if last_error is not None:
                raise last_error
            return []

        if isinstance(payload, list):
            results = payload
        else:
            results = payload.get("results") or payload.get("data") or []

        normalized: list[dict[str, Any]] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            index = item.get("index")
            score = item.get("relevance_score", item.get("score"))
            if index is None or score is None:
                continue
            normalized.append({"index": int(index), "score": float(score)})
        return normalized
