from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from ai_orchestrator.services.agent_tool_calling_probe import (
    AgentToolCallingProbe,
    AgentToolCallingProbeError,
)


class Command(BaseCommand):
    help = "Verify Gemma 4 exposes a native OpenAI-compatible tool call through llama.cpp."

    def add_arguments(self, parser):
        parser.add_argument("--base-url", default=getattr(settings, "AGENT_RUNTIME_BASE_URL", ""))
        parser.add_argument("--api-key", default=getattr(settings, "AGENT_RUNTIME_API_KEY", ""))
        parser.add_argument("--model", default=getattr(settings, "AGENT_RUNTIME_MODEL", ""))
        parser.add_argument("--expected-tool", default="search_deals")
        parser.add_argument(
            "--expected-build",
            default=getattr(settings, "AGENT_RUNTIME_EXPECTED_SERVER_BUILD", ""),
        )
        parser.add_argument("--timeout", type=float, default=120)

    def handle(self, *args, **options):
        try:
            result = AgentToolCallingProbe(
                base_url=options["base_url"],
                api_key=options["api_key"],
                model=options["model"],
                timeout_seconds=options["timeout"],
            ).run(
                expected_tool=options["expected_tool"],
                expected_build=options["expected_build"],
            )
        except AgentToolCallingProbeError as exc:
            raise CommandError(f"Agent tool-calling readiness failed: {exc}") from exc

        self.stdout.write(
            self.style.SUCCESS(
                "Agent tool-calling readiness passed: "
                f"model={result.model}, build={result.server_build or 'unknown'}, "
                f"tool={result.tool_name}, arguments={result.arguments}."
            )
        )
