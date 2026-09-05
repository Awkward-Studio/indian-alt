"""Typed contracts and configuration for the production agent runtime."""

from .authorization import AgentAuthorizationError, AgentAuthorizationService
from .catalog import LoadedSkill, SkillCatalog, SkillSummary
from .config import AgentRuntimeSettings
from .contracts import (
    AgentBudget,
    AgentCitation,
    AgentDependencies,
    AgentExecutionResult,
    AgentEvent,
    AgentEventType,
    AgentOutput,
    AgentRequest,
    AgentTerminalReason,
    AgentUsage,
)
from .runtime import AgentRuntime, AgentRuntimeFailure, AgentRuntimeFactory
from .shadow import AgentShadowRunner, ShadowMetrics

__all__ = [
    "AgentBudget",
    "AgentAuthorizationError",
    "AgentAuthorizationService",
    "AgentCitation",
    "AgentDependencies",
    "AgentExecutionResult",
    "AgentEvent",
    "AgentEventType",
    "AgentOutput",
    "AgentRequest",
    "AgentRuntime",
    "AgentRuntimeFailure",
    "AgentRuntimeFactory",
    "AgentRuntimeSettings",
    "AgentShadowRunner",
    "AgentTerminalReason",
    "AgentUsage",
    "ShadowMetrics",
    "SkillCatalog",
    "SkillSummary",
    "LoadedSkill",
]
