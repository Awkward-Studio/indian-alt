from io import StringIO

from django.conf import settings
from django.core.management import BaseCommand, call_command


class Command(BaseCommand):
    help = "Run a read-only, lightweight universal-chat smoke test against configurable inference endpoints."

    def add_arguments(self, parser):
        parser.add_argument("query", help="Question to run through planner, retrieval, reranking, and answer generation.")
        parser.add_argument("--vllm-base-url", help="Text vLLM OpenAI-compatible base URL, including /v1.")
        parser.add_argument("--vllm-api-key", help="Text vLLM API key.")
        parser.add_argument("--text-model", help="Text/planner model ID reported by vLLM /v1/models.")
        parser.add_argument("--embedding-base-url", help="Embedding OpenAI-compatible base URL, including /v1.")
        parser.add_argument("--embedding-model", help="Embedding model ID.")
        parser.add_argument("--reranker-base-url", help="Reranker base URL.")
        parser.add_argument("--reranker-model", help="Reranker model ID.")
        parser.add_argument("--skip-rerank", action="store_true", help="Skip the reranker endpoint.")
        parser.add_argument("--no-analysis", action="store_true", help="Stop after planner and retrieval instead of generating an answer.")
        parser.add_argument("--analysis-max-tokens", type=int, default=512, help="Maximum answer tokens for the smoke test.")

    def handle(self, *args, **options):
        overrides = {
            "VLLM_BASE_URL": options.get("vllm_base_url"),
            "VLLM_API_KEY": options.get("vllm_api_key"),
            "VLLM_TEXT_MODEL": options.get("text_model"),
            "VLLM_PLANNER_MODEL": options.get("text_model"),
            "EMBEDDING_BASE_URL": options.get("embedding_base_url"),
            "EMBEDDING_MODEL": options.get("embedding_model"),
            "RERANKER_BASE_URL": options.get("reranker_base_url"),
            "RERANKER_MODEL": options.get("reranker_model"),
        }
        original_values = {name: getattr(settings, name) for name in overrides}

        try:
            for name, value in overrides.items():
                if value:
                    setattr(settings, name, value.rstrip("/") if name.endswith("_URL") else value)

            self.stdout.write(
                "Running universal-chat smoke test "
                f"against {settings.VLLM_BASE_URL} with model {settings.VLLM_TEXT_MODEL}."
            )
            captured_output = StringIO()
            call_command(
                "inspect_universal_chat_query",
                options["query"],
                light=True,
                run_analysis=not options["no_analysis"],
                stop_after="analysis" if not options["no_analysis"] else "context",
                analysis_max_tokens=options["analysis_max_tokens"],
                compact_output=True,
                diagnose_live=True,
                skip_rerank=options["skip_rerank"],
                stdout=captured_output,
                stderr=self.stderr,
            )
            self.stdout.write(captured_output.getvalue())
        finally:
            for name, value in original_values.items():
                setattr(settings, name, value)
