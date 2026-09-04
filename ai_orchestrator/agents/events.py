from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic_ai import (
    AgentRunResultEvent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
)

from .contracts import AgentEvent, AgentEventType, AgentTerminalReason


class AgentEventAdapter:
    """Translate SDK events into the stable event contract exposed by the app."""

    def __init__(self, run_id: UUID):
        self.run_id = run_id
        self.sequence = 0

    def started(self) -> AgentEvent:
        return self._event(AgentEventType.RUN_STARTED)

    def failed(self, reason: AgentTerminalReason, message: str) -> AgentEvent:
        return self._event(
            AgentEventType.RUN_FAILED,
            {"message": message[:2_000]},
            terminal_reason=reason,
        )

    def completed(self, output: dict[str, Any], usage: dict[str, int]) -> list[AgentEvent]:
        events = [
            self._event(
                AgentEventType.CITATION_ADDED,
                {"citation": citation},
            )
            for citation in output.get("citations", [])
        ]
        events.append(
            self._event(
                AgentEventType.RUN_COMPLETED,
                {"output": output, "usage": usage},
                terminal_reason=AgentTerminalReason.COMPLETED,
            )
        )
        return events

    def translate(self, sdk_event: Any) -> AgentEvent | None:
        if isinstance(sdk_event, PartStartEvent) and isinstance(sdk_event.part, TextPart):
            if sdk_event.part.content:
                return self._event(
                    AgentEventType.MODEL_DELTA,
                    {"text": sdk_event.part.content},
                )
        if isinstance(sdk_event, PartDeltaEvent) and isinstance(sdk_event.delta, TextPartDelta):
            if sdk_event.delta.content_delta:
                return self._event(
                    AgentEventType.MODEL_DELTA,
                    {"text": sdk_event.delta.content_delta},
                )
        if isinstance(sdk_event, FunctionToolCallEvent):
            return self._event(
                AgentEventType.TOOL_STARTED,
                {
                    "tool_name": sdk_event.part.tool_name,
                    "tool_call_id": sdk_event.part.tool_call_id,
                },
            )
        if isinstance(sdk_event, FunctionToolResultEvent):
            return self._event(
                AgentEventType.TOOL_COMPLETED,
                {
                    "tool_name": getattr(sdk_event.part, "tool_name", ""),
                    "tool_call_id": getattr(sdk_event.part, "tool_call_id", ""),
                    "outcome": getattr(sdk_event.part, "outcome", "success"),
                },
            )
        # Thinking parts and final SDK wrappers are deliberately not exposed.
        if isinstance(sdk_event, AgentRunResultEvent):
            return None
        return None

    def _event(
        self,
        event_type: AgentEventType,
        public_payload: dict[str, Any] | None = None,
        *,
        terminal_reason: AgentTerminalReason | None = None,
    ) -> AgentEvent:
        event = AgentEvent(
            run_id=self.run_id,
            sequence=self.sequence,
            event_type=event_type,
            occurred_at=datetime.now(UTC),
            public_payload=public_payload or {},
            terminal_reason=terminal_reason,
        )
        self.sequence += 1
        return event
