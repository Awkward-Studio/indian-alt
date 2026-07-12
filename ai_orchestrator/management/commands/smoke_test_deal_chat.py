import uuid

from django.conf import settings
from django.core.management import BaseCommand

from ai_orchestrator.services.ai_processor import AIProcessorService
from ai_orchestrator.services.universal_chat import UniversalChatService
from ai_orchestrator.tasks import DEAL_CHAT_CONVERSATIONAL_PROMPT
from deals.models import Deal


class Command(BaseCommand):
    help = "Run the automatic single-deal chat path without deal-helper selection."

    def add_arguments(self, parser):
        parser.add_argument("deal_id", help="Deal UUID to query.")
        parser.add_argument("query", help="Question to ask about the deal.")
        parser.add_argument("--vllm-base-url", required=True, help="Text vLLM OpenAI-compatible base URL, including /v1.")
        parser.add_argument("--vllm-api-key", required=True, help="Text vLLM API key.")
        parser.add_argument("--text-model", required=True, help="Text model ID.")
        parser.add_argument("--embedding-base-url", required=True, help="Embedding base URL, including /v1.")
        parser.add_argument("--embedding-model", required=True, help="Embedding model ID.")
        parser.add_argument("--reranker-base-url", help="Optional reranker base URL.")
        parser.add_argument("--reranker-model", help="Optional reranker model ID.")
        parser.add_argument("--max-tokens", type=int, default=512, help="Maximum answer tokens for the smoke test.")

    def handle(self, *args, **options):
        deal = Deal.objects.get(id=options["deal_id"])
        overrides = {
            "VLLM_BASE_URL": options["vllm_base_url"].rstrip("/"),
            "VLLM_API_KEY": options["vllm_api_key"],
            "VLLM_TEXT_MODEL": options["text_model"],
            "VLLM_PLANNER_MODEL": options["text_model"],
            "EMBEDDING_BASE_URL": options["embedding_base_url"].rstrip("/"),
            "EMBEDDING_MODEL": options["embedding_model"],
            "RERANKER_BASE_URL": (options.get("reranker_base_url") or "").rstrip("/"),
            "RERANKER_MODEL": options.get("reranker_model") or "",
        }
        originals = {name: getattr(settings, name) for name in overrides}

        try:
            for name, value in overrides.items():
                setattr(settings, name, value)

            ai_service = AIProcessorService()
            chat_service = UniversalChatService(ai_service)
            metadata = chat_service.process_single_deal_build_metadata(
                options["query"],
                conversation_id=str(uuid.uuid4()),
                history_context="",
                audit_log_id=None,
                deal_id=str(deal.id),
            )
            metadata.update(
                {
                    "model_provider": "vllm",
                    "personality_only_system": True,
                    "prompt_template_override": DEAL_CHAT_CONVERSATIONAL_PROMPT,
                    "max_tokens": max(1, int(options["max_tokens"])),
                }
            )

            result = ai_service.process_content(
                content=options["query"],
                skill_name="deal_chat",
                source_type="deal_chat_smoke",
                source_id=str(deal.id),
                metadata=metadata,
                stream=False,
            )
            self.stdout.write(self.style.SUCCESS(f"Automatic deal chat passed for {deal.title}."))
            self.stdout.write(
                f"Retrieved chunks={metadata.get('retrieved_chunk_count', 0)} | "
                f"selected chunks={metadata.get('selected_chunk_count', 0)} | "
                f"context chars={len(metadata.get('context_data') or '')}"
            )
            self.stdout.write(result.get("response") or "[empty response]")
        finally:
            for name, value in originals.items():
                setattr(settings, name, value)
