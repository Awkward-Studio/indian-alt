import json
import time

from django.conf import settings
from django.core.management.base import BaseCommand

from deals.services.competitor_web_research import CompetitorWebResearchService


class Command(BaseCommand):
    help = "Test the grounded two-query SearXNG and local-model competitor pipeline"

    def add_arguments(self, parser):
        parser.add_argument("--company", default="Zepto", help="Company to research")
        parser.add_argument("--sector", default="", help="Optional company sector used to focus the query")
        parser.add_argument("--industry", default="", help="Optional company industry used to focus the query")
        parser.add_argument("--location", default="India", help="Optional target market or company location")
        parser.add_argument("--summary", default="", help="Optional short business description")
        parser.add_argument("--random", action="store_true", help="Test five random deals from the database")
        parser.add_argument(
            "--lm-url",
            default=getattr(settings, "VLLM_BASE_URL", "http://localhost:8000/v1"),
            help="Override the OpenAI-compatible local model URL",
        )
        parser.add_argument(
            "--model",
            default=getattr(settings, "VLLM_TEXT_MODEL", "") or "local-model",
            help="Override the local model identifier",
        )
        parser.add_argument(
            "--search-url",
            default=getattr(settings, "SEARXNG_BASE_URL", "http://localhost:8081"),
            help="Override the SearXNG URL",
        )
        parser.add_argument(
            "--min-competitors",
            type=int,
            default=3,
            help="Minimum evidence-backed competitors required for the test to pass",
        )

    def handle(self, *args, **options):
        targets = self._targets(options)
        self.stdout.write(self.style.MIGRATE_HEADING("=" * 72))
        self.stdout.write(self.style.MIGRATE_HEADING("Grounded Public/Private Competitor Search Test"))
        self.stdout.write(f"SearXNG: {options['search_url']}")
        self.stdout.write(f"Inference: {options['lm_url']}")
        self.stdout.write(f"Model: {options['model']}")
        self.stdout.write(self.style.MIGRATE_HEADING("=" * 72))

        failures = 0
        for index, target in enumerate(targets, 1):
            self.stdout.write(self.style.WARNING(f"\n[{index}/{len(targets)}] {target['company_name']}"))
            service = CompetitorWebResearchService()
            service.search_service.base_url = options["search_url"].rstrip("/")
            service.llm_service.base_url = options["lm_url"].rstrip("/")
            service.model = options["model"]
            started_at = time.monotonic()
            try:
                result = service.research(**target)
            except Exception as exc:
                failures += 1
                self.stderr.write(self.style.ERROR(f"FAILED: {exc}"))
                continue

            competitors = result.get("competitors", [])
            diagnostics = result.get("diagnostics", {})
            elapsed = time.monotonic() - started_at
            self.stdout.write(json.dumps({"competitors": competitors}, indent=2, ensure_ascii=False))
            self.stdout.write(self.style.SUCCESS(
                f"Completed in {elapsed:.2f}s: {len(competitors)} unique candidates, "
                f"{diagnostics.get('search_sources', 0)} sources from "
                f"{diagnostics.get('search_requests', 0)} SearXNG requests, "
                f"{diagnostics.get('page_fetches', 0)} fetched pages, and "
                f"{diagnostics.get('evidence_sources', 0)} prompt evidence sources."
            ))
            if diagnostics.get("search_requests") != 2:
                failures += 1
                self.stderr.write(self.style.ERROR("Pipeline did not use exactly two SearXNG requests."))
            elif len(competitors) < max(1, options["min_competitors"]):
                failures += 1
                self.stderr.write(self.style.ERROR(
                    result.get("message")
                    or f"Expected at least {options['min_competitors']} grounded competitors; got {len(competitors)}."
                ))
            elif any(not item.get("evidence_urls") for item in competitors):
                failures += 1
                self.stderr.write(self.style.ERROR("At least one competitor has no returned search evidence URL."))

        if failures:
            raise RuntimeError(f"{failures} local search test(s) did not return grounded competitors")

    @staticmethod
    def _targets(options):
        if not options.get("random"):
            return [{
                "company_name": options["company"],
                "sector": options.get("sector", ""),
                "industry": options.get("industry", ""),
                "location": options.get("location", ""),
                "business_summary": options.get("summary", ""),
            }]

        from deals.models import Deal

        deals = Deal.objects.exclude(title__isnull=True).exclude(title="").order_by("?")[:5]
        return [{
            "company_name": deal.title,
            "sector": deal.sector or "",
            "industry": deal.industry or "",
            "location": ", ".join(value for value in [deal.city, deal.country] if value),
            "business_summary": deal.deal_summary or "",
        } for deal in deals]
