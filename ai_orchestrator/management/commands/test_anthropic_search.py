import json
import time
from django.core.management.base import BaseCommand
from django.conf import settings
from ai_orchestrator.services.llm_providers import AnthropicProviderService

class Command(BaseCommand):
    help = "Test dynamic model upgrading and native web search capabilities with Anthropic Claude API"

    def add_arguments(self, parser):
        parser.add_argument(
            "--prompt",
            type=str,
            help="Custom prompt to test. Dynamic model routing will auto-detect search intent."
        )
        parser.add_argument(
            "--stream",
            action="store_true",
            help="Test streaming responses chunk-by-chunk"
        )
        parser.add_argument(
            "--haiku",
            type=str,
            help="Override default CLAUDE_TEXT_MODEL for testing"
        )
        parser.add_argument(
            "--sonnet",
            type=str,
            help="Override default CLAUDE_SEARCH_MODEL for testing"
        )

    def mask_key(self, key: str) -> str:
        if not key:
            return "None/Empty"
        if len(key) <= 8:
            return "***"
        return f"{key[:7]}...{key[-5:]}"

    def run_inference(self, service, prompt, stream, label):
        self.stdout.write(self.style.MIGRATE_HEADING(f"\n--- Running Inference Test: {label} ---"))
        self.stdout.write(f"  User Prompt: '{prompt}'")
        
        payload = {
            "model": "default",
            "system": "You are a helpful investment analyst assistant.",
            "prompt": prompt,
            "options": {
                "max_tokens": 4096,
                "temperature": 0.1
            }
        }

        # Build payload preview to check dynamic upgrade and tools injection
        built_payload = service._build_anthropic_payload(payload, stream=stream)
        self.stdout.write(self.style.SUCCESS(f"  Routed Model : {built_payload.get('model')}"))
        
        tools = built_payload.get("tools") or []
        if tools:
            self.stdout.write(self.style.WARNING(f"  Web Search Tool Injected: Yes ({tools[0].get('type')})"))
        else:
            self.stdout.write(f"  Web Search Tool Injected: No")

        self.stdout.write("  Executing API call...")
        self.stdout.write("-" * 60)

        start_time = time.time()
        try:
            if stream:
                thinking_captured = ""
                response_captured = ""
                first_chunk = True
                
                for chunk in service.execute_stream(payload):
                    chunk_data = json.loads(chunk)
                    if chunk_data.get("done"):
                        break
                        
                    thinking_delta = chunk_data.get("thinking", "")
                    response_delta = chunk_data.get("response", "")
                    
                    if thinking_delta:
                        if first_chunk:
                            self.stdout.write(self.style.WARNING("=== Thinking Delta ==="))
                            first_chunk = False
                        thinking_captured += thinking_delta
                        self.stdout.write(thinking_delta, ending="")
                        self.stdout.flush()
                        
                    if response_delta:
                        if not first_chunk and thinking_captured and not response_captured:
                            self.stdout.write("\n")
                            self.stdout.write(self.style.SUCCESS("=== Response Delta ==="))
                        elif first_chunk and not response_captured:
                            self.stdout.write(self.style.SUCCESS("=== Response Delta ==="))
                            first_chunk = False
                            
                        response_captured += response_delta
                        self.stdout.write(response_delta, ending="")
                        self.stdout.flush()
                
                self.stdout.write("\n" + "-" * 60)
                elapsed = time.time() - start_time
                self.stdout.write(self.style.SUCCESS(f"SUCCESS: Completed in {elapsed:.3f}s"))
                self.stdout.write(f"  Thinking Length: {len(thinking_captured)} chars")
                self.stdout.write(f"  Response Length: {len(response_captured)} chars")
            else:
                result = service.execute_standard(payload)
                elapsed = time.time() - start_time
                
                thinking = result.get("thinking", "")
                response = result.get("response", "")
                usage = result.get("usage", {})

                if thinking:
                    self.stdout.write(self.style.WARNING("=== Thinking ==="))
                    self.stdout.write(thinking)
                    self.stdout.write("-" * 60)

                self.stdout.write(self.style.SUCCESS("=== Response ==="))
                self.stdout.write(response)
                self.stdout.write("-" * 60)

                self.stdout.write(self.style.SUCCESS(f"SUCCESS: Completed in {elapsed:.3f}s"))
                if usage:
                    self.stdout.write(f"  Tokens: Input: {usage.get('input_tokens')}, Output: {usage.get('output_tokens')}")

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"INFERENCE ERROR: {e}"))

    def handle(self, *args, **options):
        self.stdout.write(self.style.MIGRATE_HEADING("=" * 60))
        self.stdout.write(self.style.MIGRATE_HEADING("Anthropic Claude Dynamic Web Search & Upgrader Test"))
        self.stdout.write(self.style.MIGRATE_HEADING("=" * 60))

        # Initialize the service
        service = AnthropicProviderService()

        # Handle overrides
        if options["haiku"]:
            service.model = options["haiku"]
        if options["sonnet"]:
            service.search_model = options["sonnet"]

        # Diagnostics
        self.stdout.write(self.style.WARNING("1. Configuration Diagnostics:"))
        self.stdout.write(f"  Anthropic API Key   : {self.mask_key(service.api_key)}")
        self.stdout.write(f"  Default Text Model  : {service.model}")
        self.stdout.write(f"  Search/Sonnet Model : {service.search_model}")
        
        if not service.api_key:
            self.stdout.write(self.style.ERROR("\nERROR: Anthropic API Key is missing or empty!"))
            self.stdout.write("Please configure ANTHROPIC_API_KEY in your settings/.env file.")
            self.stdout.write(self.style.MIGRATE_HEADING("=" * 60))
            return

        # Check connectivity
        self.stdout.write(self.style.WARNING("\n2. Network Reachability Health Check:"))
        health = service.health_check()
        if health:
            self.stdout.write(self.style.SUCCESS("  SUCCESS: Anthropic API host is reachable."))
        else:
            self.stdout.write(self.style.ERROR("  FAILURE: Could not reach api.anthropic.com."))
            return

        # Run test cycles
        stream = options["stream"]
        custom_prompt = options["prompt"]

        if custom_prompt:
            # Custom prompt execution
            self.run_inference(service, custom_prompt, stream, "Custom User Query")
        else:
            # Cycle 1: General Query (No search intent - should stay on Haiku)
            prompt_haiku = "Who wrote the play Hamlet? Answer in exactly 5 words."
            self.run_inference(service, prompt_haiku, stream, "Cycle 1: General Query (Haiku)")

            # Cycle 2: Web Search Query (Has search intent - should upgrade to Sonnet + attach Web Search tool)
            prompt_sonnet = "What is the latest news or exit transaction announced for India Alternatives in 2026? Summarize in 3 sentences."
            self.run_inference(service, prompt_sonnet, stream, "Cycle 2: Web Search Query (Sonnet + Search Tool)")

        self.stdout.write(self.style.MIGRATE_HEADING("\n" + "=" * 60))
