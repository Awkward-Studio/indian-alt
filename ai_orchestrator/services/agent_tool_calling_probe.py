from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse
import json

import requests


class AgentToolCallingProbeError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentToolCallingProbeResult:
    model: str
    server_build: str
    model_path: str
    tool_name: str
    arguments: dict[str, Any]


class AgentToolCallingProbe:
    """Check agent readiness without changing ordinary inference health."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 120,
    ):
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise AgentToolCallingProbeError("The inference base URL must be absolute HTTP or HTTPS.")
        if not model.strip():
            raise AgentToolCallingProbeError("A model name is required for the tool-call probe.")
        self.base_url = base_url.rstrip("/")
        self.server_url = self.base_url.removesuffix("/v1")
        self.api_key = api_key
        self.model = model.strip()
        self.timeout_seconds = max(1.0, min(float(timeout_seconds), 600.0))

    @property
    def headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def run(self, *, expected_tool: str, expected_build: str = "") -> AgentToolCallingProbeResult:
        properties = self._properties()
        server_build = str(properties.get("build_info") or "")
        model_path = str(properties.get("model_path") or "")
        chat_template = str(properties.get("chat_template") or "")

        if expected_build and server_build != expected_build:
            raise AgentToolCallingProbeError(
                f"Expected llama.cpp build {expected_build}, received {server_build or 'unknown'}."
            )
        if "gemma-4" not in model_path.lower():
            raise AgentToolCallingProbeError(
                f"The live model is not Gemma 4: {model_path or 'model_path missing from /props'}."
            )
        if "<|tool_call>" not in chat_template:
            raise AgentToolCallingProbeError(
                "The live chat template does not contain Gemma 4 tool-call tokens."
            )

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        'Call search_deals with query "India alternatives". '
                        "Do not answer without calling the tool."
                    ),
                }
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": expected_tool,
                        "description": "Search the authorized deal list by company or investment theme.",
                        "parameters": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                            "required": ["query"],
                            "additionalProperties": False,
                        },
                    },
                }
            ],
            "tool_choice": "auto",
            "temperature": 0,
            "max_tokens": 256,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        response = requests.post(
            f"{self.base_url}/chat/completions",
            headers=self.headers,
            json=payload,
            timeout=(3.0, self.timeout_seconds),
        )
        self._raise_for_status(response, "tool-call request")
        body = self._json(response, "tool-call response")
        choice = (body.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        tool_calls = message.get("tool_calls") or []
        if choice.get("finish_reason") != "tool_calls" or not tool_calls:
            raise AgentToolCallingProbeError(
                "Gemma 4 returned no OpenAI-compatible tool call. "
                f"finish_reason={choice.get('finish_reason')!r}."
            )

        function = tool_calls[0].get("function") or {}
        tool_name = str(function.get("name") or "")
        if tool_name != expected_tool:
            raise AgentToolCallingProbeError(
                f"Expected tool {expected_tool}, received {tool_name or 'an empty name'}."
            )
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except (TypeError, json.JSONDecodeError) as exc:
            raise AgentToolCallingProbeError("Tool arguments were not valid JSON.") from exc
        if not isinstance(arguments, dict) or not str(arguments.get("query") or "").strip():
            raise AgentToolCallingProbeError("Tool arguments did not contain a non-empty query.")

        return AgentToolCallingProbeResult(
            model=self.model,
            server_build=server_build,
            model_path=model_path,
            tool_name=tool_name,
            arguments=arguments,
        )

    def _properties(self) -> dict[str, Any]:
        response = requests.get(
            f"{self.server_url}/props",
            headers=self.headers,
            timeout=(3.0, min(self.timeout_seconds, 30.0)),
        )
        self._raise_for_status(response, "llama.cpp /props request")
        return self._json(response, "llama.cpp /props response")

    @staticmethod
    def _raise_for_status(response, label: str) -> None:
        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            raise AgentToolCallingProbeError(f"{label} failed: {exc}") from exc

    @staticmethod
    def _json(response, label: str) -> dict[str, Any]:
        try:
            value = response.json()
        except ValueError as exc:
            raise AgentToolCallingProbeError(f"{label} was not JSON.") from exc
        if not isinstance(value, dict):
            raise AgentToolCallingProbeError(f"{label} must be a JSON object.")
        return value
