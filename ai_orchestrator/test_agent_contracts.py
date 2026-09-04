from importlib.metadata import version
from types import SimpleNamespace
from uuid import uuid4

from django.test import SimpleTestCase, override_settings
from pydantic import ValidationError

from ai_orchestrator.agents import (
    AgentBudget,
    AgentDependencies,
    AgentEvent,
    AgentEventType,
    AgentRequest,
    AgentRuntimeSettings,
)


class AgentRuntimeSettingsTests(SimpleTestCase):
    def test_pydantic_ai_runtime_dependency_is_pinned(self):
        import pydantic_ai  # noqa: F401

        self.assertEqual(version("pydantic-ai-slim"), "2.38.0")

    def test_agent_runtime_is_disabled_by_default(self):
        runtime = AgentRuntimeSettings.from_django_settings(
            SimpleNamespace(VLLM_BASE_URL="http://vllm:8000/v1")
        )

        self.assertFalse(runtime.enabled)
        self.assertFalse(runtime.shadow_enabled)
        self.assertEqual(runtime.http_max_retries, 0)
        self.assertIsNone(runtime.default_budget.total_tokens_limit)

    def test_active_runtime_requires_a_model(self):
        source = SimpleNamespace(
            AGENT_RUNTIME_ENABLED=True,
            AGENT_RUNTIME_BASE_URL="http://vllm:8000/v1",
            AGENT_RUNTIME_MODEL="",
        )

        with self.assertRaisesRegex(ValidationError, "AGENT_RUNTIME_MODEL"):
            AgentRuntimeSettings.from_django_settings(source)

    def test_api_key_is_not_exposed_by_repr(self):
        runtime = AgentRuntimeSettings(
            base_url="http://vllm:8000/v1",
            api_key="top-secret",
        )

        self.assertNotIn("top-secret", repr(runtime))

    @override_settings(
        AGENT_RUNTIME_ENABLED=True,
        AGENT_RUNTIME_MODEL="gemma-4-12b-it-q8",
        AGENT_RUNTIME_BASE_URL="http://vllm:8000/v1/",
        AGENT_RUNTIME_TOTAL_TOKENS_LIMIT=12_000,
    )
    def test_django_settings_are_normalized(self):
        runtime = AgentRuntimeSettings.from_django_settings()

        self.assertTrue(runtime.enabled)
        self.assertEqual(runtime.model, "gemma-4-12b-it-q8")
        self.assertEqual(runtime.base_url, "http://vllm:8000/v1")
        self.assertEqual(runtime.default_budget.total_tokens_limit, 12_000)


class AgentContractTests(SimpleTestCase):
    def test_request_scope_can_only_narrow_server_scope(self):
        permitted = uuid4()
        forbidden = uuid4()
        dependencies = AgentDependencies(
            requested_by_id=7,
            allowed_deal_ids={permitted},
            capability_ids={"deals.read", "documents.search"},
            audit_log_id=uuid4(),
        )
        request = AgentRequest(
            prompt="  Compare these deals  ",
            requested_deal_ids={permitted, forbidden},
            requested_capability_ids={"deals.read", "shell.execute"},
        )

        self.assertEqual(request.prompt, "Compare these deals")
        self.assertEqual(request.effective_deal_ids(dependencies), {permitted})
        self.assertEqual(request.effective_capability_ids(dependencies), {"deals.read"})

    def test_contracts_are_immutable_and_reject_unknown_fields(self):
        budget = AgentBudget()

        with self.assertRaises(ValidationError):
            AgentBudget(tool_call_limit=True)
        with self.assertRaises(ValidationError):
            AgentBudget(unknown_limit=5)
        with self.assertRaises(ValidationError):
            budget.tool_call_limit = 99

    def test_capability_identifiers_have_a_restricted_alphabet(self):
        with self.assertRaisesRegex(ValidationError, "lowercase"):
            AgentDependencies(
                requested_by_id=7,
                capability_ids={"Documents.Search"},
                audit_log_id=uuid4(),
            )

    def test_public_events_reject_private_payload_fields(self):
        with self.assertRaisesRegex(ValidationError, "private keys"):
            AgentEvent(
                run_id=uuid4(),
                sequence=1,
                event_type=AgentEventType.MODEL_DELTA,
                occurred_at="2026-09-04T12:00:00Z",
                public_payload={"raw_thinking": "hidden"},
            )
