import asyncio
from uuid import uuid4

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase

from ai_orchestrator.agents import (
    AgentDependencies,
    AgentExecutionResult,
    AgentOutput,
    AgentRequest,
    AgentRuntimeSettings,
    AgentShadowRunner,
    AgentUsage,
)
from ai_orchestrator.agents.shadow import AuditLogShadowMetricRecorder, ShadowMetrics
from ai_orchestrator.models import AIAuditLog


class RecordingMetricSink:
    def __init__(self):
        self.items = []

    async def record(self, dependencies, metrics):
        self.items.append((dependencies, metrics))


class StubRuntime:
    async def run(self):
        return AgentExecutionResult(
            run_id=uuid4(),
            conversation_id="shadow-conversation",
            output=AgentOutput(answer="Primary answer", citations=[], warnings=[]),
            usage=AgentUsage(requests=1, tool_calls=2, total_tokens=30),
        )


class CountingFactory:
    def __init__(self):
        self.calls = 0

    def create(self, request, dependencies):
        self.calls += 1
        return StubRuntime()


class AgentShadowRunnerTests(SimpleTestCase):
    def setUp(self):
        self.request = AgentRequest(prompt="Compare this deal")
        self.dependencies = AgentDependencies(
            requested_by_id=7,
            audit_log_id=uuid4(),
        )

    def test_disabled_shadow_produces_zero_agent_calls_and_metrics(self):
        factory = CountingFactory()
        sink = RecordingMetricSink()
        runner = AgentShadowRunner(
            AgentRuntimeSettings(base_url="http://inference:8000/v1"),
            runtime_factory=factory,
            recorder=sink,
        )

        metrics = asyncio.run(
            runner.run(self.request, self.dependencies, primary_answer="Primary answer")
        )

        self.assertIsNone(metrics)
        self.assertEqual(factory.calls, 0)
        self.assertEqual(sink.items, [])

    def test_shadow_records_comparison_metrics_without_answer_text(self):
        factory = CountingFactory()
        sink = RecordingMetricSink()
        runner = AgentShadowRunner(
            AgentRuntimeSettings(
                shadow_enabled=True,
                base_url="http://inference:8000/v1",
                model="gemma-4-12b-it-q8",
            ),
            runtime_factory=factory,
            recorder=sink,
        )

        metrics = asyncio.run(
            runner.run(self.request, self.dependencies, primary_answer="Primary answer")
        )

        self.assertEqual(factory.calls, 1)
        self.assertEqual(metrics.status, "completed")
        self.assertTrue(metrics.matches_primary)
        self.assertEqual(metrics.tool_calls, 2)
        self.assertNotIn("Primary answer", metrics.model_dump_json())
        self.assertEqual(sink.items[0][1], metrics)


class AuditLogShadowMetricRecorderTests(TestCase):
    def test_metrics_are_visible_on_only_the_originating_user_bound_audit(self):
        user = User.objects.create_user(username="shadow-owner")
        audit = AIAuditLog.objects.create(
            source_type="agent_shadow",
            requested_by=user,
            model_used="primary-model",
            system_prompt="",
            user_prompt="",
            raw_response="",
            source_metadata={"existing": "preserved"},
        )
        dependencies = AgentDependencies(
            requested_by_id=user.id,
            audit_log_id=audit.id,
        )
        metrics = ShadowMetrics(
            status="completed",
            duration_ms=12,
            requests=1,
            total_tokens=20,
            answer_length=40,
            matches_primary=False,
            terminal_reason="completed",
        )

        AuditLogShadowMetricRecorder._record_sync(dependencies, metrics)
        audit.refresh_from_db()

        self.assertEqual(audit.source_metadata["existing"], "preserved")
        self.assertEqual(audit.source_metadata["agent_shadow"], metrics.model_dump(mode="json"))
        self.assertNotIn("answer", audit.source_metadata["agent_shadow"])
