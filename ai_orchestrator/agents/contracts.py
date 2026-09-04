from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AgentContract(BaseModel):
    """Base policy for values crossing the agent runtime boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class AgentTerminalReason(StrEnum):
    COMPLETED = "completed"
    DISABLED = "disabled"
    INVALID_REQUEST = "invalid_request"
    MODEL_ERROR = "model_error"
    OUTPUT_VALIDATION_FAILED = "output_validation_failed"
    REQUEST_LIMIT_EXCEEDED = "request_limit_exceeded"
    TOOL_CALL_LIMIT_EXCEEDED = "tool_call_limit_exceeded"
    TOKEN_LIMIT_EXCEEDED = "token_limit_exceeded"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class AgentEventType(StrEnum):
    RUN_STARTED = "run_started"
    MODEL_DELTA = "model_delta"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    CITATION_ADDED = "citation_added"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"


class AgentBudget(AgentContract):
    model_request_limit: int = Field(default=8, ge=1, le=64)
    tool_call_limit: int = Field(default=16, ge=0, le=256)
    total_tokens_limit: int | None = Field(default=None, ge=1, le=10_000_000)
    request_timeout_seconds: float = Field(default=600, gt=0, le=3_600)
    tool_result_max_chars: int = Field(default=20_000, ge=1_000, le=200_000)

    @field_validator(
        "model_request_limit", "tool_call_limit", "total_tokens_limit", mode="before"
    )
    @classmethod
    def reject_boolean_integers(cls, value: int | None) -> int | None:
        if isinstance(value, bool):
            raise ValueError("boolean values are not valid integer limits")
        return value


class AgentDependencies(AgentContract):
    """Server-created authorization and provenance for one run."""

    requested_by_id: int = Field(gt=0)
    allowed_deal_ids: frozenset[UUID] = Field(default_factory=frozenset, max_length=100)
    capability_ids: frozenset[str] = Field(default_factory=frozenset, max_length=64)
    tool_result_max_chars: int = Field(default=20_000, ge=1_000, le=200_000)
    audit_log_id: UUID
    conversation_id: UUID | None = None

    @field_validator("requested_by_id", mode="before")
    @classmethod
    def reject_boolean_user_id(cls, value: int) -> int:
        if isinstance(value, bool):
            raise ValueError("requested_by_id must be an integer user identifier")
        return value

    @field_validator("tool_result_max_chars", mode="before")
    @classmethod
    def reject_boolean_tool_result_limit(cls, value: int) -> int:
        if isinstance(value, bool):
            raise ValueError("tool_result_max_chars must be an integer")
        return value

    @field_validator("capability_ids")
    @classmethod
    def validate_capability_ids(cls, values: frozenset[str]) -> frozenset[str]:
        for value in values:
            if not value or len(value) > 100:
                raise ValueError("capability IDs must contain between 1 and 100 characters")
            if any(character not in "abcdefghijklmnopqrstuvwxyz0123456789._-" for character in value):
                raise ValueError("capability IDs must use lowercase letters, numbers, '.', '_', or '-'")
        return values


class AgentRequest(AgentContract):
    """Validated internal request. User identity belongs in AgentDependencies."""

    prompt: str = Field(min_length=1, max_length=100_000)
    requested_deal_ids: frozenset[UUID] = Field(default_factory=frozenset, max_length=100)
    requested_capability_ids: frozenset[str] = Field(default_factory=frozenset, max_length=64)
    budget: AgentBudget = Field(default_factory=AgentBudget)

    @field_validator("prompt")
    @classmethod
    def normalize_prompt(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("prompt must not be blank")
        return normalized

    def effective_deal_ids(self, dependencies: AgentDependencies) -> frozenset[UUID]:
        """Never permit request data to expand the server-created deal scope."""

        return self.requested_deal_ids & dependencies.allowed_deal_ids

    def effective_capability_ids(self, dependencies: AgentDependencies) -> frozenset[str]:
        """Never permit request data to expand the server-created capability scope."""

        return self.requested_capability_ids & dependencies.capability_ids


class AgentCitation(AgentContract):
    source_id: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=500)
    excerpt: str = Field(default="", max_length=2_000)
    locator: str = Field(default="", max_length=2_000)


class AgentOutput(AgentContract):
    answer: str = Field(min_length=1, max_length=200_000)
    citations: tuple[AgentCitation, ...] = Field(default_factory=tuple, max_length=100)
    warnings: tuple[str, ...] = Field(default_factory=tuple, max_length=50)


class AgentEvent(AgentContract):
    """Application-owned event envelope, independent of SDK event classes."""

    run_id: UUID
    sequence: int = Field(ge=0)
    event_type: AgentEventType
    occurred_at: datetime
    public_payload: dict[str, Any] = Field(default_factory=dict)
    terminal_reason: AgentTerminalReason | None = None

    @field_validator("public_payload")
    @classmethod
    def reject_private_payload_keys(cls, value: dict[str, Any]) -> dict[str, Any]:
        forbidden = {"api_key", "raw_thinking", "system_prompt", "user_prompt"}

        def keys(payload: Any) -> set[str]:
            if isinstance(payload, dict):
                return set(payload) | set().union(*(keys(child) for child in payload.values()))
            if isinstance(payload, (list, tuple)):
                return set().union(*(keys(child) for child in payload))
            return set()

        found = sorted(forbidden & keys(value))
        if found:
            raise ValueError(f"public_payload contains private keys: {', '.join(found)}")
        return value


class AgentUsage(AgentContract):
    requests: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)


class AgentExecutionResult(AgentContract):
    run_id: UUID
    conversation_id: str
    output: AgentOutput
    usage: AgentUsage
    terminal_reason: AgentTerminalReason = AgentTerminalReason.COMPLETED
