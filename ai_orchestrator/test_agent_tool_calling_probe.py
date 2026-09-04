from io import StringIO
from unittest.mock import Mock, patch

from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, override_settings

from ai_orchestrator.services.agent_tool_calling_probe import (
    AgentToolCallingProbe,
    AgentToolCallingProbeError,
)


def response(payload, *, status_code=200):
    value = Mock(status_code=status_code)
    value.json.return_value = payload
    value.raise_for_status.return_value = None
    return value


class AgentToolCallingProbeTests(SimpleTestCase):
    @patch("ai_orchestrator.services.agent_tool_calling_probe.requests.post")
    @patch("ai_orchestrator.services.agent_tool_calling_probe.requests.get")
    def test_probe_validates_props_and_native_tool_call(self, get, post):
        get.return_value = response(
            {
                "model_path": "/models/gemma-4-12B-it-Q8_0.gguf",
                "build_info": "b10795-6703d7894",
                "chat_template": "<|tool_call>call:test{}<tool_call|>",
            }
        )
        post.return_value = response(
            {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "search_deals",
                                        "arguments": '{"query":"India alternatives"}',
                                    }
                                }
                            ]
                        },
                    }
                ]
            }
        )

        result = AgentToolCallingProbe(
            base_url="http://inference:8000/v1",
            api_key="secret",
            model="gemma-4-12b-it-q8",
        ).run(expected_tool="search_deals", expected_build="b10795-6703d7894")

        self.assertEqual(result.tool_name, "search_deals")
        self.assertEqual(result.arguments, {"query": "India alternatives"})
        self.assertEqual(get.call_args.args[0], "http://inference:8000/props")
        self.assertEqual(post.call_args.kwargs["json"]["tool_choice"], "auto")

    @patch("ai_orchestrator.services.agent_tool_calling_probe.requests.post")
    @patch("ai_orchestrator.services.agent_tool_calling_probe.requests.get")
    def test_probe_rejects_plain_text_instead_of_a_tool_call(self, get, post):
        get.return_value = response(
            {
                "model_path": "/models/gemma-4-12B-it-Q8_0.gguf",
                "build_info": "build-1",
                "chat_template": "<|tool_call>",
            }
        )
        post.return_value = response(
            {"choices": [{"finish_reason": "stop", "message": {"content": "No tool"}}]}
        )

        with self.assertRaisesRegex(AgentToolCallingProbeError, "no OpenAI-compatible"):
            AgentToolCallingProbe(
                base_url="http://inference:8000/v1",
                api_key="",
                model="gemma-4-12b-it-q8",
            ).run(expected_tool="search_deals")

    @override_settings(
        AGENT_RUNTIME_BASE_URL="http://inference:8000/v1",
        AGENT_RUNTIME_API_KEY="",
        AGENT_RUNTIME_MODEL="gemma-4-12b-it-q8",
    )
    @patch("ai_orchestrator.management.commands.smoke_test_agent_tool_calling.AgentToolCallingProbe.run")
    def test_command_turns_probe_failure_into_command_error(self, run):
        run.side_effect = AgentToolCallingProbeError("template missing")

        with self.assertRaisesRegex(CommandError, "template missing"):
            call_command("smoke_test_agent_tool_calling", stdout=StringIO())
