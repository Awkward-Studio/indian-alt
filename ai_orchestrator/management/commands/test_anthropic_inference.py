import json
import time
from django.core.management.base import BaseCommand
from django.conf import settings
from ai_orchestrator.services.llm_providers import AnthropicProviderService

class Command(BaseCommand):
    help = "Test connection, configuration, and inference with Anthropic Claude API"

    def add_arguments(self, parser):
        parser.add_argument(
            "--key",
            type=str,
            help="Anthropic API Key (overrides settings.ANTHROPIC_API_KEY)"
        )
        parser.add_argument(
            "--model",
            type=str,
            help="Claude model to target (overrides settings.CLAUDE_TEXT_MODEL)"
        )
        parser.add_argument(
            "--prompt",
            type=str,
            default="Hello, Claude! Confirm you are online, state your model name, and verify if thinking budget is enabled.",
            help="Prompt to send for testing inference"
        )
        parser.add_argument(
            "--stream",
            action="store_true",
            help="Test streaming inference chunk-by-chunk"
        )
        parser.add_argument(
            "--system",
            type=str,
            default="You are a helpful investment analyst assistant.",
            help="System prompt to configure context"
        )

    def mask_key(self, key: str) -> str:
        if not key:
            return "None/Empty"
        if len(key) <= 8:
            return "***"
        return f"{key[:7]}...{key[-5:]}"

    def handle(self, *args, **options):
        self.stdout.write(self.style.MIGRATE_HEADING("=" * 60))
        self.stdout.write(self.style.MIGRATE_HEADING("Starting Anthropic Claude Integration & Inference Test"))
        self.stdout.write(self.style.MIGRATE_HEADING("=" * 60))

        # Initialize the service
        service = AnthropicProviderService()

        # Resolve API key and Model
        api_key = options["key"] or service.api_key
        model = options["model"] or service.model
        prompt = options["prompt"]
        system_prompt = options["system"]

        # Override service key/model if provided
        if options["key"]:
            service.api_key = api_key
        if options["model"]:
            service.model = model

        # 1. Config Check
        self.stdout.write(self.style.WARNING("1. Configuration Diagnostics:"))
        self.stdout.write(f"  Configured API Key : {self.mask_key(api_key)}")
        self.stdout.write(f"  Target Text Model  : {model}")
        self.stdout.write(f"  Available Models   : {', '.join(service.get_available_models())}")
        
        if not api_key:
            self.stdout.write(self.style.ERROR("\nERROR: Anthropic API Key is missing or empty!"))
            self.stdout.write("Please configure ANTHROPIC_API_KEY in your settings/.env file:")
            self.stdout.write("  ANTHROPIC_API_KEY=your_key_here")
            self.stdout.write(self.style.MIGRATE_HEADING("=" * 60))
            return

        # 2. Network Reachability / Health Check
        self.stdout.write(self.style.WARNING("\n2. Network Reachability Health Check:"))
        self.stdout.write("  Checking connection to api.anthropic.com...")
        health = service.health_check()
        if health:
            self.stdout.write(self.style.SUCCESS("  SUCCESS: Anthropic API host is reachable."))
        else:
            self.stdout.write(self.style.ERROR("  FAILURE: Could not reach api.anthropic.com. Check your network or firewall rules."))

        # 3. Running Inference
        self.stdout.write(self.style.WARNING(f"\n3. Running Inference (stream={options['stream']}):"))
        payload = {
            "model": model,
            "system": system_prompt,
            "prompt": prompt,
            "options": {
                "max_tokens": 4096,
                "temperature": 0.1
            }
        }
        
        self.stdout.write(f"  System prompt: {system_prompt}")
        self.stdout.write(f"  User prompt  : {prompt}")
        self.stdout.write("  Sending request to Anthropic Claude...")
        self.stdout.write("-" * 60)

        start_time = time.time()
        try:
            if options["stream"]:
                thinking_captured = ""
                response_captured = ""
                
                self.stdout.write(self.style.WARNING("=== Thinking Delta (Claude 3.7 Budget Mode) ==="))
                first_chunk = True
                
                for chunk in service.execute_stream(payload):
                    chunk_data = json.loads(chunk)
                    if chunk_data.get("done"):
                        break
                        
                    thinking_delta = chunk_data.get("thinking", "")
                    response_delta = chunk_data.get("response", "")
                    
                    if thinking_delta:
                        thinking_captured += thinking_delta
                        self.stdout.write(thinking_delta, ending="")
                        self.stdout.flush()
                        
                    if response_delta:
                        if first_chunk and thinking_captured:
                            self.stdout.write("\n")
                            self.stdout.write(self.style.SUCCESS("=== Response Delta ==="))
                            first_chunk = False
                        response_captured += response_delta
                        self.stdout.write(response_delta, ending="")
                        self.stdout.flush()
                
                self.stdout.write("\n" + "-" * 60)
                elapsed = time.time() - start_time
                self.stdout.write(self.style.SUCCESS(f"SUCCESS: Inference completed in {elapsed:.3f}s"))
                self.stdout.write(f"  Thinking Length: {len(thinking_captured)} chars")
                self.stdout.write(f"  Response Length: {len(response_captured)} chars")

            else:
                result = service.execute_standard(payload)
                elapsed = time.time() - start_time
                
                thinking = result.get("thinking", "")
                response = result.get("response", "")
                usage = result.get("usage", {})

                if thinking:
                    self.stdout.write(self.style.WARNING("=== Thinking (Claude 3.7 Budget Mode) ==="))
                    self.stdout.write(thinking)
                    self.stdout.write("-" * 60)

                self.stdout.write(self.style.SUCCESS("=== Response ==="))
                self.stdout.write(response)
                self.stdout.write("-" * 60)

                self.stdout.write(self.style.SUCCESS(f"SUCCESS: Inference completed in {elapsed:.3f}s"))
                if usage:
                    self.stdout.write(f"  Token Usage: Input: {usage.get('input_tokens')}, Output: {usage.get('output_tokens')}")

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"\nINFERENCE ERROR: {e}"))
            self.stdout.write("Verify that your API key is correct and active, and that your account has available quota/funds.")

        self.stdout.write(self.style.MIGRATE_HEADING("=" * 60))
