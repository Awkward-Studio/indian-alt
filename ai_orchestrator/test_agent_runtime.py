import asyncio
from uuid import uuid4

from django.test import SimpleTestCase
from pydantic import ValidationError
from pydantic_ai.models.test import TestModel
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded

from ai_orchestrator.agents import (
    AgentBudget,
    AgentDependencies,
    AgentEventType,
    AgentRequest,
    AgentRuntime,
    AgentRuntimeFailure,
    AgentRuntimeFactory,
    AgentRuntimeSettings,
    AgentTerminalReason,
)
from ai_orchestrator.agents.events import AgentEventAdapter


def active_settings(**updates):
    values = {
        "enabled": True,
        "base_url": "http://inference:8000/v1",
        "model": "gemma-4-12b-it-q8",
    }
    values.update(updates)
    return AgentRuntimeSettings(**values)


def dependencies(**updates):
    values = {"requested_by_id": 7, "audit_log_id": uuid4()}
    values.update(updates)
    return AgentDependencies(**values)


class AgentRuntimeFactoryTests(SimpleTestCase):
    def test_disabled_runtime_does_not_construct_a_model(self):
        called = False

        def model_factory(_settings):
            nonlocal called
            called = True
            return TestModel()

        factory = AgentRuntimeFactory(
            AgentRuntimeSettings(base_url="http://inference:8000/v1"),
            model_factory=model_factory,
        )

        with self.assertRaisesRegex(AgentRuntimeFailure, "disabled") as raised:
            factory.create(AgentRequest(prompt="test"), dependencies())

        self.assertEqual(raised.exception.reason, AgentTerminalReason.DISABLED)
        self.assertFalse(called)

    def test_completion_uses_injected_model_and_returns_typed_output(self):
        model = TestModel(
            custom_output_args={
                "answer": "The evidence is incomplete.",
                "citations": [],
                "warnings": ["No source was selected."],
            }
        )
        runtime = AgentRuntimeFactory(
            active_settings(),
            model_factory=lambda _settings: model,
        ).create(AgentRequest(prompt="Summarize the deal"), dependencies())

        result = runtime.run_sync()

        self.assertEqual(result.output.answer, "The evidence is incomplete.")
        self.assertEqual(result.terminal_reason, AgentTerminalReason.COMPLETED)
        self.assertGreaterEqual(result.usage.requests, 1)

    def test_factory_rejects_prompt_over_configured_limit(self):
        factory = AgentRuntimeFactory(
            active_settings(max_prompt_chars=1_000),
            model_factory=lambda _settings: TestModel(),
        )

        with self.assertRaisesRegex(AgentRuntimeFailure, "character limit") as raised:
            factory.create(AgentRequest(prompt="x" * 1_001), dependencies())

        self.assertEqual(raised.exception.reason, AgentTerminalReason.INVALID_REQUEST)

    def test_unknown_or_unauthorized_capability_is_rejected_before_model_creation(self):
        factory = AgentRuntimeFactory(
            active_settings(),
            model_factory=lambda _settings: TestModel(),
        )
        request = AgentRequest(prompt="test", requested_capability_ids={"shell.execute"})

        with self.assertRaises(PermissionError):
            factory.create(request, dependencies(capability_ids={"deals.read"}))

    def test_unknown_authorized_capability_is_rejected_before_model_creation(self):
        called = False

        def model_factory(_settings):
            nonlocal called
            called = True
            return TestModel()

        factory = AgentRuntimeFactory(active_settings(), model_factory=model_factory)
        request = AgentRequest(prompt="test", requested_capability_ids={"unknown.read"})

        with self.assertRaises(LookupError):
            factory.create(request, dependencies(capability_ids={"unknown.read"}))

        self.assertFalse(called)

    def test_request_narrows_deal_scope_and_propagates_tool_result_limit(self):
        allowed = uuid4()
        omitted = uuid4()
        runtime = AgentRuntimeFactory(
            active_settings(),
            model_factory=lambda _settings: TestModel(),
        ).create(
            AgentRequest(
                prompt="test",
                requested_deal_ids={allowed},
                budget=AgentBudget(tool_result_max_chars=1_234),
            ),
            dependencies(allowed_deal_ids={allowed, omitted}),
        )

        self.assertEqual(runtime.dependencies.allowed_deal_ids, {allowed})
        self.assertEqual(runtime.dependencies.tool_result_max_chars, 1_234)

    def test_limit_and_validation_failures_have_deterministic_reasons(self):
        cases = (
            (UsageLimitExceeded("request limit exceeded"), AgentTerminalReason.REQUEST_LIMIT_EXCEEDED),
            (UsageLimitExceeded("tool call limit exceeded"), AgentTerminalReason.TOOL_CALL_LIMIT_EXCEEDED),
            (UsageLimitExceeded("total token limit exceeded"), AgentTerminalReason.TOKEN_LIMIT_EXCEEDED),
            (UnexpectedModelBehavior("invalid output"), AgentTerminalReason.OUTPUT_VALIDATION_FAILED),
        )

        for error, expected in cases:
            with self.subTest(error=error):
                self.assertEqual(AgentRuntime._failure(error).reason, expected)


class AgentRuntimeStreamingTests(SimpleTestCase):
    def test_stream_has_ordered_start_and_terminal_events(self):
        runtime = AgentRuntimeFactory(
            active_settings(),
            model_factory=lambda _settings: TestModel(
                custom_output_args={"answer": "Done", "citations": [], "warnings": []}
            ),
        ).create(AgentRequest(prompt="Complete this"), dependencies())

        async def collect():
            return [event async for event in runtime.stream_events()]

        events = asyncio.run(collect())

        self.assertEqual(events[0].event_type, AgentEventType.RUN_STARTED)
        self.assertEqual(events[-1].event_type, AgentEventType.RUN_COMPLETED)
        self.assertEqual([event.sequence for event in events], list(range(len(events))))

    def test_event_contract_rejects_nested_private_payload(self):
        adapter = AgentEventAdapter(uuid4())

        with self.assertRaisesRegex(ValidationError, "private keys"):
            adapter.completed(
                {"answer": "No", "citations": [], "metadata": {"raw_thinking": "hidden"}},
                {},
            )

    def test_timeout_maps_to_public_terminal_reason(self):
        class SlowAgent:
            async def run(self, *args, **kwargs):
                await asyncio.sleep(0.02)

        runtime = AgentRuntimeFactory(
            active_settings(),
            model_factory=lambda _settings: TestModel(),
            agent_builder=lambda _model, _toolsets, _settings: SlowAgent(),
        ).create(
            AgentRequest(
                prompt="wait",
                budget=AgentBudget(request_timeout_seconds=0.001),
            ),
            dependencies(),
        )

        with self.assertRaises(AgentRuntimeFailure) as raised:
            asyncio.run(runtime.run())

        self.assertEqual(raised.exception.reason, AgentTerminalReason.TIMED_OUT)
