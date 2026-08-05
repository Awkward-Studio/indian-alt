"""Run semantic deal-pipeline fixtures against the configured T4 model."""

import json
import time
from pathlib import Path
from urllib.parse import urlsplit

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from ai_orchestrator.services.ai_processor import AIProcessorService
from ai_orchestrator.services.llm_providers import VLLMProviderService


SCENARIOS = {
    "new_deal": {
        "content": (
            "Acme Circular is raising INR 125 crore for a new recycling plant. "
            "FY26 revenue was INR 80 crore and the lead banker is Maya Rao."
        ),
        "facts": ["Acme Circular", "125", "recycling", "80", "Maya Rao"],
        "route": "PROPOSE_NEW",
    },
    "enrichment": {
        "content": (
            "Delta update for the existing Acme Circular deal: FY26 revenue was revised "
            "to INR 84 crore and customer concentration is 22 percent. Attachment: FY26 MIS.pdf."
        ),
        "facts": ["84", "customer concentration", "22", "FY26 MIS"],
        "route": "ENRICH_EXISTING",
    },
    "meeting": {
        "content": (
            "Meeting summary for Acme Circular. Management confirmed the Pune plant starts "
            "in October 2026. Action item: Maya will send the capex schedule by Friday."
        ),
        "facts": ["Pune", "October 2026", "capex", "Friday"],
        "route": "UNIQUE_DEAL_MATCH",
    },
}


class Command(BaseCommand):
    help = "Validate new-deal, enrichment, attachment, and meeting semantics on the configured T4 VM."

    def add_arguments(self, parser):
        parser.add_argument("--scenario", action="append", dest="scenarios")
        parser.add_argument("--report-json")

    def handle(self, *args, **options):
        selected = options.get("scenarios") or list(SCENARIOS)
        if "all" in selected:
            selected = list(SCENARIOS)
        unknown = sorted(set(selected) - set(SCENARIOS))
        if unknown:
            raise CommandError(f"Unknown scenario(s): {', '.join(unknown)}")

        provider = VLLMProviderService()
        report = {
            "status": "running",
            "endpoint": self._sanitized_endpoint(getattr(settings, "VLLM_BASE_URL", "")),
            "configured_model": getattr(settings, "VLLM_TEXT_MODEL", ""),
            "scenarios": [],
        }
        try:
            if not provider.health_check():
                raise CommandError("T4 inference health check failed")
            available_models = sorted(provider.get_available_models())
            report["available_models"] = available_models
            configured = report["configured_model"]
            if configured and available_models and configured not in available_models:
                raise CommandError(f"Configured model {configured!r} is not advertised by the endpoint")

            service = AIProcessorService()
            for name in selected:
                case = SCENARIOS[name]
                started = time.monotonic()
                failures = []
                try:
                    normalized = service.process_content(
                        content=case["content"],
                        skill_name="document_normalization",
                        source_type="deal_pipeline_t4_test",
                        metadata={
                            "chat_template_kwargs": {"enable_thinking": False},
                            "max_tokens": 2048,
                            "request_timeout": 180,
                        },
                    )
                    if not isinstance(normalized, dict):
                        failures.append(f"normalization returned {type(normalized).__name__}, expected object")
                    serialized = json.dumps(normalized, default=str).casefold()
                    missing = [fact for fact in case["facts"] if fact.casefold() not in serialized]
                    if missing:
                        failures.append(f"normalized evidence omitted semantic facts: {missing}")
                except Exception as exc:
                    normalized = {}
                    failures.append(self._sanitize_error(str(exc)))

                result = {
                    "name": name,
                    "passed": not failures,
                    "expected_route": case["route"],
                    "required_semantic_facts": case["facts"],
                    "latency_ms": round((time.monotonic() - started) * 1000),
                    "normalization_keys": sorted(normalized) if isinstance(normalized, dict) else [],
                    "failures": failures,
                }
                report["scenarios"].append(result)
                style = self.style.SUCCESS if result["passed"] else self.style.ERROR
                self.stdout.write(style(f"{name}: {'PASS' if result['passed'] else 'FAIL'} ({result['latency_ms']} ms)"))

            report["status"] = "passed" if all(item["passed"] for item in report["scenarios"]) else "failed"
        except Exception as exc:
            report["status"] = "failed"
            report["error"] = self._sanitize_error(str(exc))

        report_path = options.get("report_json")
        if report_path:
            Path(report_path).write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
            self.stdout.write(f"Report: {report_path}")

        if report["status"] != "passed":
            failures = [item["name"] for item in report["scenarios"] if not item["passed"]]
            raise CommandError(report.get("error") or f"T4 deal pipeline scenarios failed: {', '.join(failures)}")
        self.stdout.write(self.style.SUCCESS(f"All {len(report['scenarios'])} T4 deal pipeline scenarios passed."))

    @staticmethod
    def _sanitized_endpoint(url):
        parsed = urlsplit(url or "")
        if not parsed.scheme or not parsed.hostname:
            return "unconfigured"
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{parsed.hostname}{port}{parsed.path.rstrip('/')}"

    @staticmethod
    def _sanitize_error(message):
        import re

        message = re.sub(r"https?://[^\s]+", "[endpoint]", message or "")
        message = re.sub(r"[\w.+-]+@[\w.-]+", "[email]", message)
        return message[:1000]
