from __future__ import annotations

import time
from typing import Protocol

from asgiref.sync import sync_to_async
from django.db import transaction
from pydantic import BaseModel, ConfigDict, Field

from ai_orchestrator.models import AIAuditLog

from .config import AgentRuntimeSettings
from .contracts import AgentDependencies, AgentRequest
from .runtime import AgentRuntimeFactory


class ShadowMetrics(BaseModel):
    """Non-sensitive comparison data; shadow answer text is never persisted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: str
    duration_ms: int = Field(ge=0)
    requests: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    answer_length: int = Field(default=0, ge=0)
    matches_primary: bool | None = None
    terminal_reason: str


class ShadowMetricRecorder(Protocol):
    async def record(self, dependencies: AgentDependencies, metrics: ShadowMetrics) -> None: ...


class AuditLogShadowMetricRecorder:
    async def record(self, dependencies: AgentDependencies, metrics: ShadowMetrics) -> None:
        await sync_to_async(self._record_sync, thread_sensitive=True)(dependencies, metrics)

    @staticmethod
    def _record_sync(dependencies: AgentDependencies, metrics: ShadowMetrics) -> None:
        with transaction.atomic():
            audit = AIAuditLog.objects.select_for_update().filter(
                id=dependencies.audit_log_id,
                requested_by_id=dependencies.requested_by_id,
            ).first()
            if audit is None:
                return
            audit.source_metadata = {
                **(audit.source_metadata or {}),
                "agent_shadow": metrics.model_dump(mode="json"),
            }
            audit.save(update_fields=["source_metadata"])


class AgentShadowRunner:
    """Execute an isolated comparison run without changing the primary response."""

    def __init__(
        self,
        settings: AgentRuntimeSettings,
        *,
        runtime_factory: AgentRuntimeFactory | None = None,
        recorder: ShadowMetricRecorder | None = None,
    ):
        self.settings = settings
        self.runtime_factory = runtime_factory or AgentRuntimeFactory(settings)
        self.recorder = recorder or AuditLogShadowMetricRecorder()

    async def run(
        self,
        request: AgentRequest,
        dependencies: AgentDependencies,
        *,
        primary_answer: str,
    ) -> ShadowMetrics | None:
        if not self.settings.shadow_enabled:
            return None

        started = time.monotonic()
        try:
            result = await self.runtime_factory.create(request, dependencies).run()
            normalized_primary = " ".join(primary_answer.split()).casefold()
            normalized_shadow = " ".join(result.output.answer.split()).casefold()
            metrics = ShadowMetrics(
                status="completed",
                duration_ms=int((time.monotonic() - started) * 1_000),
                requests=result.usage.requests,
                tool_calls=result.usage.tool_calls,
                total_tokens=result.usage.total_tokens,
                answer_length=len(result.output.answer),
                matches_primary=normalized_primary == normalized_shadow,
                terminal_reason=result.terminal_reason.value,
            )
        except Exception as exc:
            reason = getattr(getattr(exc, "reason", None), "value", "model_error")
            metrics = ShadowMetrics(
                status="failed",
                duration_ms=int((time.monotonic() - started) * 1_000),
                terminal_reason=reason,
            )
        await self.recorder.record(dependencies, metrics)
        return metrics
