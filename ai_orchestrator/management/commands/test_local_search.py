import json
import time
from django.core.management.base import BaseCommand
from django.conf import settings
from ai_orchestrator.services.search_provider import SearXNGProviderService
from ai_orchestrator.services.llm_providers import VLLMProviderService

class Command(BaseCommand):
    help = "Test SearXNG local web search and output generation via local LM Studio / VLLM model"

    def add_arguments(self, parser):
        parser.add_argument(
            "--prompt",
            type=str,
            help="Custom search prompt to test.",
            default="What is India Alternatives private equity?"
        )
        parser.add_argument(
            "--lm-url",
            type=str,
            help="Override local LM Studio or VLLM base URL",
            default=getattr(settings, "LM_STUDIO_BASE_URL", "") or getattr(settings, "VLLM_BASE_URL", "http://localhost:1234/v1")
        )
        parser.add_argument(
            "--search-url",
            type=str,
            help="Override local SearXNG base URL",
            default=getattr(settings, "SEARXNG_BASE_URL", "http://localhost:8081")
        )

    def handle(self, *args, **options):
        self.stdout.write(self.style.MIGRATE_HEADING("=" * 60))
        self.stdout.write(self.style.MIGRATE_HEADING("Local SearXNG & Local AI Pipeline Test"))
        self.stdout.write(self.style.MIGRATE_HEADING("=" * 60))

        prompt = options["prompt"]
        lm_url = options["lm_url"]
        search_url = options["search_url"]

        self.stdout.write(self.style.WARNING(f"\n1. Executing Local Web Search via SearXNG ({search_url})"))
        self.stdout.write(f"  Query: '{prompt}'")
        
        start_time = time.time()
        search_service = SearXNGProviderService()
        search_service.base_url = search_url.rstrip("/")
        
        try:
            search_context = search_service.search(prompt, num_results=5)
            elapsed = time.time() - start_time
            self.stdout.write(self.style.SUCCESS(f"  SUCCESS: Search completed in {elapsed:.3f}s"))
            self.stdout.write(f"  Extracted Context Length: {len(search_context)} chars\n")
            
            # Print a snippet of the context
            snippet = search_context[:500].replace("\n", " ") + "..." if len(search_context) > 500 else search_context
            self.stdout.write(self.style.HTTP_INFO(f"  Context Snippet: {snippet}\n"))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"  SEARCH ERROR: {e}"))
            return

        self.stdout.write(self.style.WARNING(f"\n2. Generating Output via Local AI ({lm_url})"))
        
        # Configure the VLLM Provider for the local LM Studio instance
        ai_service = VLLMProviderService()
        ai_service.base_url = lm_url
        ai_service.api_key = "lm-studio" # Standard dummy key for LM studio

        augmented_prompt = f"Using ONLY the following web search context:\n{search_context}\n\nPlease summarize the findings for the query: '{prompt}'"
        
        payload = {
            "model": "local-model",
            "system": "You are a helpful investment analyst assistant. Synthesize the provided web search context.",
            "prompt": augmented_prompt,
            "options": {
                "temperature": 0.1
            }
        }

        self.stdout.write("  Executing Local AI Inference...")
        self.stdout.write("-" * 60)
        
        start_time = time.time()
        try:
            result = ai_service.execute_standard(payload, timeout=120)
            elapsed = time.time() - start_time
            
            response = result.get("response", "")
            usage = result.get("usage", {})

            self.stdout.write(self.style.SUCCESS("=== Response ==="))
            self.stdout.write(response)
            self.stdout.write("-" * 60)
            self.stdout.write(self.style.SUCCESS(f"SUCCESS: AI Generation completed in {elapsed:.3f}s"))
            if usage:
                self.stdout.write(f"  Tokens: Input: {usage.get('prompt_tokens', usage.get('input_tokens'))}, Output: {usage.get('completion_tokens', usage.get('output_tokens'))}")

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"  INFERENCE ERROR: {e}"))

        self.stdout.write(self.style.MIGRATE_HEADING("\n" + "=" * 60))
