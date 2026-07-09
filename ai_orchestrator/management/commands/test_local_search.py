import json
import time
from django.core.management.base import BaseCommand
from django.conf import settings
from ai_orchestrator.services.search_provider import SearXNGProviderService
from ai_orchestrator.services.llm_providers import VLLMProviderService

class Command(BaseCommand):
    help = "Test SearXNG local web search and output generation via local LM Studio / VLLM model for Competitor Search"

    def add_arguments(self, parser):
        parser.add_argument(
            "--company",
            type=str,
            help="Target company to find competitors for.",
            default="Zepto"
        )
        parser.add_argument(
            "--random",
            action="store_true",
            help="Run sequentially for 5 random deals from the DB."
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
        self.stdout.write(self.style.MIGRATE_HEADING("Local SearXNG & Local AI Pipeline Test: COMPETITOR SEARCH"))
        self.stdout.write(self.style.MIGRATE_HEADING("=" * 60))

        lm_url = options["lm_url"]
        search_url = options["search_url"]

        if options.get("random"):
            from deals.models import Deal
            deals = Deal.objects.exclude(title__isnull=True).exclude(title='').order_by('?')[:5]
            companies = [deal.title for deal in deals]
            if not companies:
                self.stdout.write(self.style.ERROR("No deals found in database."))
                return
        else:
            companies = [options["company"]]

        for idx, company_name in enumerate(companies, 1):
            self.stdout.write(self.style.WARNING(f"\n[{idx}/{len(companies)}] Testing Company: {company_name}"))
            self.run_test_for_company(company_name, lm_url, search_url)

    def run_test_for_company(self, company_name, lm_url, search_url):
        search_query = f"{company_name} top competitors or similar companies india"
        self.stdout.write(self.style.WARNING(f"\n1. Executing Local Web Search via SearXNG ({search_url})"))
        self.stdout.write(f"  Query: '{search_query}'")
        
        start_time = time.time()
        search_service = SearXNGProviderService()
        search_service.base_url = search_url.rstrip("/")
        
        try:
            search_context = search_service.search(search_query, num_results=5)
            elapsed = time.time() - start_time
            self.stdout.write(self.style.SUCCESS(f"  SUCCESS: Search completed in {elapsed:.3f}s"))
            self.stdout.write(f"  Extracted Context Length: {len(search_context)} chars\n")
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"  SEARCH ERROR: {e}"))
            return

        self.stdout.write(self.style.WARNING(f"\n2. Generating Competitor JSON Output via Local AI ({lm_url})"))
        
        ai_service = VLLMProviderService()
        ai_service.base_url = lm_url
        ai_service.api_key = "lm-studio"

        prompt = (
            f"You are a sophisticated investment research assistant.\n"
            f"Run a concise web search to identify the top 10 competitors or peer companies for '{company_name}'.\n"
            f"Context details of the target company:\n"
            f"- Target Company: {company_name}\n"
            f"- Industry/Sector: Technology / Startups\n"
            f"- Location: India\n"
            f"- Existing candidates to avoid duplicating: None\n\n"
            f"Prioritize speed. This pass is ONLY for identifying competitor names and short rationales. "
            f"Also classify every competitor as either a listed public company or a private/unlisted company using obvious public-market evidence. "
            f"Do not search MCA records and do not perform detailed financial extraction in this pass. "
            f"Return exactly one JSON object and no markdown. Use this shape:\n"
            f"{{\n"
            f"  \"competitors\": [\n"
            f"    {{\n"
            f"      \"company_name\": \"Exact company or brand name\",\n"
            f"      \"core_business\": \"Short phrase only\",\n"
            f"      \"nature_of_competition\": \"Short phrase only\",\n"
            f"      \"country_or_region\": \"Primary country or region, if relevant\",\n"
            f"      \"company_type\": \"listed_public | private\",\n"
            f"      \"classification_confidence\": 0.85,\n"
            f"      \"exchange\": \"NSE/BSE or blank\",\n"
            f"      \"ticker\": \"Listed ticker/symbol or blank\",\n"
            f"      \"screener_url\": \"Screener URL if confidently known, else blank\",\n"
            f"      \"classification_source\": \"Short reason for public/private classification\"\n"
            f"    }}\n"
            f"  ]\n"
            f"}}\n"
            f"List exactly 10 companies. Do not search for or return MCA identifiers. "
            f"Keep every text field short so the full JSON response fits in the answer. "
            f"Do not include long descriptions, citations, tables, or explanatory text."
        )

        augmented_prompt = f"Using ONLY the following web search context:\n{search_context}\n\n{prompt}"
        
        payload = {
            "model": "local-model",
            "system": "You are a helpful investment analyst assistant who conducts thorough peer and competitor research based on the provided search context.",
            "prompt": augmented_prompt,
            "options": {
                "temperature": 0.0
            }
        }

        self.stdout.write("  Executing Local AI Inference...")
        self.stdout.write("-" * 60)
        
        start_time = time.time()
        try:
            result = ai_service.execute_standard(payload, timeout=600)
            elapsed = time.time() - start_time
            
            response = result.get("response", "")
            usage = result.get("usage", {})

            self.stdout.write(self.style.SUCCESS("=== Response JSON ==="))
            self.stdout.write(response)
            self.stdout.write("-" * 60)
            self.stdout.write(self.style.SUCCESS(f"SUCCESS: AI Generation completed in {elapsed:.3f}s"))
            if usage:
                self.stdout.write(f"  Tokens: Input: {usage.get('prompt_tokens', usage.get('input_tokens'))}, Output: {usage.get('completion_tokens', usage.get('output_tokens'))}")
            
            # Verify it's parseable
            try:
                clean_response = response.strip()
                if clean_response.startswith("```json"):
                    clean_response = clean_response[7:]
                elif clean_response.startswith("```"):
                    clean_response = clean_response[3:]
                if clean_response.endswith("```"):
                    clean_response = clean_response[:-3]
                clean_response = clean_response.strip()
                
                parsed = json.loads(clean_response)
                self.stdout.write(self.style.SUCCESS(f"\nParsed {len(parsed.get('competitors', []))} competitors successfully!"))
            except json.JSONDecodeError:
                self.stdout.write(self.style.ERROR("\nERROR: Model did not return valid JSON."))

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"  INFERENCE ERROR: {e}"))

        self.stdout.write(self.style.MIGRATE_HEADING("\n" + "=" * 60))
