from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any, Protocol

import httpx
from openai import AsyncOpenAI
from pydantic_ai import Agent, AgentRunResultEvent
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.usage import UsageLimits

from .config import AgentRuntimeSettings
from .contracts import (
    AgentDependencies,
    AgentEvent,
    AgentExecutionResult,
    AgentOutput,
    AgentRequest,
    AgentTerminalReason,
    AgentUsage,
)
from .events import AgentEventAdapter
from .registry import AgentCapabilityRegistry
from .toolsets import build_default_capability_registry


class AgentRuntimeFailure(RuntimeError):
    def __init__(self, reason: AgentTerminalReason, message: str):
        super().__init__(message)
        self.reason = reason


class AgentLike(Protocol):
    async def run(self, *args, **kwargs): ...

    def run_stream_events(self, *args, **kwargs): ...


ModelFactory = Callable[[AgentRuntimeSettings], Model]
AgentBuilder = Callable[[Model, list[Any], AgentRuntimeSettings], AgentLike]


class AgentRuntimeFactory:
    """Construct the disabled-by-default production agent with injectable seams."""

    def __init__(
        self,
        runtime_settings: AgentRuntimeSettings | None = None,
        *,
        model_factory: ModelFactory | None = None,
        agent_builder: AgentBuilder | None = None,
        capability_registry: AgentCapabilityRegistry | None = None,
    ):
        self.settings = runtime_settings or AgentRuntimeSettings.from_django_settings()
        self.model_factory = model_factory or self._build_openai_chat_model
        self.agent_builder = agent_builder or self._build_agent
        self.capability_registry = capability_registry or build_default_capability_registry()

    def create(self, request: AgentRequest, dependencies: AgentDependencies) -> "AgentRuntime":
        if not self.settings.enabled and not self.settings.shadow_enabled:
            raise AgentRuntimeFailure(
                AgentTerminalReason.DISABLED,
                "Production agent execution is disabled.",
            )
        if len(request.prompt) > self.settings.max_prompt_chars:
            raise AgentRuntimeFailure(
                AgentTerminalReason.INVALID_REQUEST,
                f"Agent prompt exceeds the {self.settings.max_prompt_chars} character limit.",
            )
        requested_capabilities = request.requested_capability_ids
        unauthorized_capabilities = requested_capabilities - dependencies.capability_ids
        if unauthorized_capabilities:
            denied = ", ".join(sorted(unauthorized_capabilities))
            raise PermissionError(f"Capabilities are outside the authorized scope: {denied}")
        allowed_deal_ids = dependencies.allowed_deal_ids
        if request.requested_deal_ids:
            allowed_deal_ids = request.effective_deal_ids(dependencies)
        scoped_dependencies = dependencies.model_copy(
            update={
                "allowed_deal_ids": allowed_deal_ids,
                "capability_ids": requested_capabilities,
                "tool_result_max_chars": request.budget.tool_result_max_chars,
            }
        )
        toolsets = self.capability_registry.resolve(requested_capabilities, scoped_dependencies)
        model = self.model_factory(self.settings)
        return AgentRuntime(
            agent=self.agent_builder(model, toolsets, self.settings),
            request=request,
            dependencies=scoped_dependencies,
        )

    @staticmethod
    def _build_openai_chat_model(runtime_settings: AgentRuntimeSettings) -> Model:
        client = AsyncOpenAI(
            base_url=runtime_settings.base_url,
            api_key=runtime_settings.api_key.get_secret_value() or "local-agent-runtime",
            max_retries=runtime_settings.http_max_retries,
            timeout=httpx.Timeout(
                runtime_settings.default_budget.request_timeout_seconds,
                connect=runtime_settings.connect_timeout_seconds,
            ),
        )
        return OpenAIChatModel(
            runtime_settings.model,
            provider=OpenAIProvider(openai_client=client),
        )

    @staticmethod
    def _build_agent(
        model: Model,
        toolsets: list[Any],
        runtime_settings: AgentRuntimeSettings,
    ) -> Agent[AgentDependencies, AgentOutput]:
        return Agent(
            model,
            name="india_alternatives_agent",
            deps_type=AgentDependencies,
            output_type=AgentOutput,
            instructions=(
                "Answer only within the authorized deal and capability scope. "
                "Use available evidence tools before making deal-specific factual claims. "
                "Return concise warnings when evidence is insufficient."
            ),
            retries=runtime_settings.output_retries,
            tool_timeout=runtime_settings.default_budget.request_timeout_seconds,
            toolsets=toolsets,
        )


class AgentRuntime:
    def __init__(self, *, agent: AgentLike, request: AgentRequest, dependencies: AgentDependencies):
        self.agent = agent
        self.request = request
        self.dependencies = dependencies

    async def run(self) -> AgentExecutionResult:
        try:
            async with asyncio.timeout(self.request.budget.request_timeout_seconds):
                result = await self.agent.run(
                    self.request.prompt,
                    deps=self.dependencies,
                    run_id=str(self.dependencies.audit_log_id),
                    conversation_id=(
                        str(self.dependencies.conversation_id)
                        if self.dependencies.conversation_id
                        else None
                    ),
                    usage_limits=self._usage_limits(),
                )
        except Exception as exc:
            raise self._failure(exc) from exc
        return self._result(result)

    def run_sync(self) -> AgentExecutionResult:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.run())
        raise RuntimeError("run_sync cannot be called from an active async event loop; await run instead.")

    async def stream_events(self) -> AsyncIterator[AgentEvent]:
        adapter = AgentEventAdapter(self.dependencies.audit_log_id)
        yield adapter.started()
        try:
            async with asyncio.timeout(self.request.budget.request_timeout_seconds):
                async with self.agent.run_stream_events(
                    self.request.prompt,
                    deps=self.dependencies,
                    run_id=str(self.dependencies.audit_log_id),
                    conversation_id=(
                        str(self.dependencies.conversation_id)
                        if self.dependencies.conversation_id
                        else None
                    ),
                    usage_limits=self._usage_limits(),
                ) as events:
                    async for sdk_event in events:
                        if isinstance(sdk_event, AgentRunResultEvent):
                            result = self._result(sdk_event.result)
                            for event in adapter.completed(
                                result.output.model_dump(mode="json"),
                                result.usage.model_dump(mode="json"),
                            ):
                                yield event
                            continue
                        event = adapter.translate(sdk_event)
                        if event is not None:
                            yield event
        except Exception as exc:
            failure = self._failure(exc)
            yield adapter.failed(failure.reason, str(failure))

    def _usage_limits(self) -> UsageLimits:
        return UsageLimits(
            request_limit=self.request.budget.model_request_limit,
            tool_calls_limit=self.request.budget.tool_call_limit,
            total_tokens_limit=self.request.budget.total_tokens_limit,
            count_tokens_before_request=self.request.budget.total_tokens_limit is not None,
        )

    @staticmethod
    def _result(result) -> AgentExecutionResult:
        usage_value = result.usage
        usage = usage_value() if callable(usage_value) else usage_value
        return AgentExecutionResult(
            run_id=result.run_id,
            conversation_id=result.conversation_id,
            output=AgentOutput.model_validate(result.output),
            usage=AgentUsage(
                requests=usage.requests,
                tool_calls=usage.tool_calls,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                total_tokens=usage.total_tokens,
            ),
        )

    @staticmethod
    def _failure(exc: Exception) -> AgentRuntimeFailure:
        if isinstance(exc, AgentRuntimeFailure):
            return exc
        if isinstance(exc, TimeoutError):
            return AgentRuntimeFailure(AgentTerminalReason.TIMED_OUT, "Agent run timed out.")
        if isinstance(exc, UsageLimitExceeded):
            message = str(exc)
            lowered = message.lower()
            if "tool" in lowered:
                reason = AgentTerminalReason.TOOL_CALL_LIMIT_EXCEEDED
            elif "token" in lowered:
                reason = AgentTerminalReason.TOKEN_LIMIT_EXCEEDED
            else:
                reason = AgentTerminalReason.REQUEST_LIMIT_EXCEEDED
            return AgentRuntimeFailure(reason, message)
        if isinstance(exc, UnexpectedModelBehavior):
            return AgentRuntimeFailure(
                AgentTerminalReason.OUTPUT_VALIDATION_FAILED,
                "The model did not return a valid agent output within the retry budget.",
            )
        return AgentRuntimeFailure(AgentTerminalReason.MODEL_ERROR, str(exc) or type(exc).__name__)
