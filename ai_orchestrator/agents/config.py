from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from django.conf import settings as django_settings
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from .contracts import AgentBudget


class AgentRuntimeSettings(BaseModel):
    """Validated settings used when the production runtime is constructed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    shadow_enabled: bool = False
    base_url: str = "http://localhost:8000/v1"
    api_key: SecretStr = Field(default_factory=lambda: SecretStr(""), repr=False)
    model: str = ""
    connect_timeout_seconds: float = Field(default=2, gt=0, le=60)
    http_max_retries: int = Field(default=0, ge=0, le=5)
    output_retries: int = Field(default=2, ge=0, le=10)
    max_prompt_chars: int = Field(default=50_000, ge=1_000, le=200_000)
    default_budget: AgentBudget = Field(default_factory=AgentBudget)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        normalized = value.rstrip("/")
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute HTTP or HTTPS URL")
        return normalized

    @field_validator(
        "http_max_retries", "output_retries", "max_prompt_chars", mode="before"
    )
    @classmethod
    def reject_boolean_integers(cls, value: int) -> int:
        if isinstance(value, bool):
            raise ValueError("boolean values are not valid integer settings")
        return value

    @model_validator(mode="after")
    def require_model_when_active(self) -> "AgentRuntimeSettings":
        if (self.enabled or self.shadow_enabled) and not self.model.strip():
            raise ValueError("AGENT_RUNTIME_MODEL is required when agent execution is enabled")
        return self

    @classmethod
    def from_django_settings(cls, source: Any = None) -> "AgentRuntimeSettings":
        source = source or django_settings
        total_tokens = int(getattr(source, "AGENT_RUNTIME_TOTAL_TOKENS_LIMIT", 0) or 0)
        return cls(
            enabled=bool(getattr(source, "AGENT_RUNTIME_ENABLED", False)),
            shadow_enabled=bool(getattr(source, "AGENT_RUNTIME_SHADOW_ENABLED", False)),
            base_url=str(
                getattr(
                    source,
                    "AGENT_RUNTIME_BASE_URL",
                    getattr(source, "VLLM_BASE_URL", "http://localhost:8000/v1"),
                )
            ),
            api_key=SecretStr(
                str(
                    getattr(
                        source,
                        "AGENT_RUNTIME_API_KEY",
                        getattr(source, "VLLM_API_KEY", ""),
                    )
                )
            ),
            model=str(
                getattr(
                    source,
                    "AGENT_RUNTIME_MODEL",
                    getattr(source, "VLLM_TEXT_MODEL", ""),
                )
            ),
            connect_timeout_seconds=float(
                getattr(source, "AGENT_RUNTIME_CONNECT_TIMEOUT", 2) or 2
            ),
            http_max_retries=getattr(source, "AGENT_RUNTIME_HTTP_MAX_RETRIES", 0),
            output_retries=getattr(source, "AGENT_RUNTIME_OUTPUT_RETRIES", 2),
            max_prompt_chars=getattr(source, "AGENT_RUNTIME_MAX_PROMPT_CHARS", 50_000),
            default_budget=AgentBudget(
                model_request_limit=getattr(source, "AGENT_RUNTIME_MODEL_REQUEST_LIMIT", 8),
                tool_call_limit=getattr(source, "AGENT_RUNTIME_TOOL_CALL_LIMIT", 16),
                total_tokens_limit=total_tokens or None,
                request_timeout_seconds=float(
                    getattr(source, "AGENT_RUNTIME_REQUEST_TIMEOUT", 600) or 600
                ),
                tool_result_max_chars=getattr(
                    source, "AGENT_RUNTIME_TOOL_RESULT_MAX_CHARS", 20_000
                ),
            ),
        )
