import json

from django.core.management.base import BaseCommand, CommandError

from ai_orchestrator.agent_demo.runtime import DemoAgentRuntime


class Command(BaseCommand):
    help = "Run a small read-only agentic chat demo over existing deals/chunks."

    def add_arguments(self, parser):
        parser.add_argument("question", nargs="+", help="Question to ask the demo agent.")
        parser.add_argument(
            "--max-iterations",
            type=int,
            default=8,
            help="Maximum agent tool-loop iterations. Default: 8.",
        )
        parser.add_argument(
            "--model",
            default=None,
            help="Optional vLLM model override. Defaults to AIRuntimeService planner model.",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Print a JSON summary after the trace.",
        )

    def handle(self, *args, **options):
        question = " ".join(options["question"]).strip()
        if not question:
            raise CommandError("Question is required.")

        self.stdout.write(self.style.HTTP_INFO("Starting read-only agent demo..."))
        self.stdout.write(f"Question: {question}")

        runtime = DemoAgentRuntime(
            question=question,
            max_iterations=options["max_iterations"],
            model=options.get("model"),
            stdout=self.stdout,
        )
        result = runtime.run()

        if result.error:
            self.stdout.write("")
            self.stdout.write(self.style.ERROR(result.error))
        else:
            self.stdout.write("")
            self.stdout.write(self.style.SUCCESS("Final answer"))
            self.stdout.write(result.final_answer or "[empty answer]")

        if options["json"]:
            payload = {
                "question": result.question,
                "final_answer": result.final_answer,
                "citations": result.citations,
                "error": result.error,
                "steps": [
                    {
                        "index": step.index,
                        "action": step.action,
                        "reason": step.reason,
                        "arguments": step.arguments,
                        "observation": step.observation,
                        "duration_ms": step.duration_ms,
                        "confidence": step.confidence,
                    }
                    for step in result.steps
                ],
            }
            self.stdout.write("")
            self.stdout.write(json.dumps(payload, default=str, indent=2))
