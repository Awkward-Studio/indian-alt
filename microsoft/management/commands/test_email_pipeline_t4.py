"""Run synthetic email-thread quality cases against the configured T4 model."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone as django_timezone

from ai_orchestrator.services.ai_processor import AIProcessorService
from ai_orchestrator.services.llm_providers import VLLMProviderService
from microsoft.services.email_thread_unfolder import EmailThreadUnfolder


def build_cases() -> dict[str, list[SimpleNamespace]]:
    start = datetime(2026, 8, 5, 8, 0, tzinfo=timezone.utc)

    def message(key, subject, body="", html="", offset=0):
        return SimpleNamespace(
            id=key,
            subject=subject,
            body_text=body,
            body_html=html,
            body_preview="",
            date_received=start + timedelta(minutes=offset),
        )

    original = "ALPHA_UNIQUE_731 Initial revenue is INR 125 crore."
    reply = "BETA_UNIQUE_842 Please validate the customer concentration."
    cases = {
        "single": [message("single", "Pipeline Deal", "SINGLE_UNIQUE_620 New investment memorandum.")],
        "nested_reply": [
            message("original", "Pipeline Deal", original),
            message(
                "reply", "Re: Pipeline Deal",
                f"{reply}\n\nOn Wed, Aug 5, 2026 at 8:00 AM Banker wrote:\n{original}", offset=5,
            ),
            message(
                "nested", "Re: Pipeline Deal",
                "GAMMA_UNIQUE_953 Approved for diligence.\n\n"
                f"On Wed, Aug 5, 2026 at 8:05 AM Analyst wrote:\n{reply}\n{original}", offset=10,
            ),
        ],
        "html_reply": [
            message("html-original", "Pipeline Deal", "HTML_OLD_164 Previous terms."),
            message(
                "html-reply", "Re: Pipeline Deal", offset=5,
                html=("<p>HTML_NEW_275 Revised valuation attached.</p>"
                      "<blockquote><p>HTML_OLD_164 Previous terms.</p></blockquote>"),
            ),
        ],
        "forward": [message(
            "forward", "Fwd: Market note",
            "FORWARD_NOTE_386 Please review.\n\n---------- Original Message ----------\n"
            "FORWARDED_EVIDENCE_497 Sector growth was 18 percent.",
        )],
        "duplicate": [
            message(
                "duplicate-one", "Pipeline Deal",
                "From: Asha Mehta <asha@example.test>\nSent: Wednesday, August 5, 2026 8:00 AM\n"
                "To: Investment Team <team@example.test>\nSubject: Pipeline Deal\n\n"
                "The company reported FY26 revenue of INR 125 crore and customer concentration of 22 percent.",
            ),
            message(
                "duplicate-two", "Re: Pipeline Deal",
                "From: Asha Mehta <asha@example.test>\nSent: Wednesday, August 5, 2026 8:00 AM\n"
                "To: Investment Team <team@example.test>\nSubject: Pipeline Deal\n\n"
                "The company reported FY26 revenue of INR 125 crore and customer concentration of 22 percent.",
                offset=5,
            ),
        ],
        "long_body": [message(
            "long", "Pipeline Deal",
            "LONG_UNIQUE_619 Beginning of long evidence.\n" + ("Operational detail. " * 2300) +
            "\nLONG_END_720 End of long evidence.",
        )],
    }
    long_thread = []
    contributions = []
    for index in range(100):
        contribution = (
            f"Project Banyan update {index + 1}: monthly revenue reached INR {41 + index} crore, "
            f"active merchants reached {1200 + index * 17}, and Region-{index:03d} requires "
            f"a distribution diligence response by day {index + 1}."
        )
        contributions.append(contribution)
        body = contribution
        subject = "Project Banyan investment thread"
        if index and index % 20 == 0:
            subject = "Fwd: Project Banyan investment thread"
            body = (
                f"Forward wave {index // 20}: the external discussion is returning to the fund.\n\n"
                f"---------- Original Message ----------\n{contribution}\n\n"
                f"From: Prior Participant <prior@example.test>\nSent: Wednesday\n"
                f"Subject: Re: Project Banyan\n\n{contributions[index - 1]}"
            )
        elif index:
            subject = "Re: Project Banyan investment thread"
            body = (
                f"{contribution}\n\nOn Wed, Aug 5, 2026 at 10:{index:02d} AM Sender wrote:\n"
                f"{contributions[index - 1]}"
            )
        long_thread.append(message(f"long-thread-{index}", subject, body, offset=index))
    cases["long_thread"] = long_thread
    return cases


class Command(BaseCommand):
    help = "Validate synthetic email unfolding and cleanup/normalization against the configured T4 VM."

    def add_arguments(self, parser):
        parser.add_argument("--case", action="append", dest="cases", help="Run only the named case; repeatable.")
        parser.add_argument("--report-json", help="Write a sanitized JSON report to this path.")
        parser.add_argument("--fail-fast", action="store_true", help="Stop after the first failed case.")

    def handle(self, *args, **options):
        cases = build_cases()
        selected = options.get("cases") or list(cases)
        unknown = sorted(set(selected) - set(cases))
        if unknown:
            raise CommandError(f"Unknown case(s): {', '.join(unknown)}. Available: {', '.join(cases)}")

        provider = VLLMProviderService()
        report = {
            "status": "running",
            "endpoint": self._sanitized_endpoint(getattr(settings, "VLLM_BASE_URL", "")),
            "configured_model": getattr(settings, "VLLM_TEXT_MODEL", ""),
            "available_models": [],
            "cases": [],
        }
        report["stale_audits_closed"] = self._close_stale_test_audits()
        report_path = options.get("report_json")

        try:
            if not provider.health_check():
                raise CommandError("T4 inference health check failed.")
            report["available_models"] = sorted(provider.get_available_models())
            configured_model = report["configured_model"]
            if configured_model and report["available_models"] and configured_model not in report["available_models"]:
                raise CommandError(
                    f"Configured model {configured_model!r} is not advertised by the T4 endpoint."
                )

            ai_service = AIProcessorService()
            for case_name in selected:
                result = self._run_case(case_name, cases[case_name], ai_service)
                report["cases"].append(result)
                style = self.style.SUCCESS if result["passed"] else self.style.ERROR
                self.stdout.write(style(
                    f"{case_name}: {'PASS' if result['passed'] else 'FAIL'} "
                    f"({result['latency_ms']} ms, {result['delta_count']} deltas)"
                ))
                if not result["passed"] and options.get("fail_fast"):
                    break

            report["status"] = "passed" if all(item["passed"] for item in report["cases"]) else "failed"
        except Exception as exc:
            report["status"] = "failed"
            report["error"] = self._sanitize_error(str(exc))
        finally:
            if report_path:
                Path(report_path).write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
                self.stdout.write(f"Report: {report_path}")

        if report["status"] != "passed":
            failures = [item["name"] for item in report["cases"] if not item["passed"]]
            detail = report.get("error") or f"Failed cases: {', '.join(failures)}"
            raise CommandError(f"Email pipeline T4 validation failed. {detail}")

        self.stdout.write(self.style.SUCCESS(f"All {len(report['cases'])} T4 email pipeline cases passed."))

    def _run_case(self, name, messages, ai_service):
        started = time.monotonic()
        deltas = EmailThreadUnfolder.unfold(messages)
        non_empty = [delta for delta in deltas if delta.text]
        model_deltas = non_empty
        if name == "long_thread":
            # Run every unfolded contribution through the T4 in five complete,
            # chronological waves. This bounds individual context size while
            # verifying all 100 messages rather than sampling endpoints.
            model_deltas = []
            for wave_index in range(5):
                wave_start = wave_index * 20
                for batch_index in range(4):
                    start = wave_start + batch_index * 5
                    batch = non_empty[start:start + 5]
                    batch_text = "\n\n--- NEXT EMAIL DELTA ---\n\n".join(item.text for item in batch)
                    model_deltas.append(SimpleNamespace(
                        email_id=f"long-wave-{wave_index + 1}-batch-{batch_index + 1}",
                        text=batch_text,
                        strategy="sequential_wave_batch",
                        delta_length=len(batch_text),
                    ))
        outputs = []
        failures = []

        for delta in model_deltas:
            expected_markers = self._markers(delta.text)
            expected_facts = self._expected_facts(name, delta.email_id)
            try:
                # Production receives an already-unfolded body delta and
                # preserves it verbatim before structured normalization.
                cleaned = delta.text
                normalized = ai_service.process_content(
                    content=cleaned,
                    skill_name="document_normalization",
                    source_type="email_pipeline_t4_test",
                    metadata={
                        "chat_template_kwargs": {"enable_thinking": False},
                        "max_tokens": 2048,
                        "request_timeout": 180,
                    },
                )
                normalized_text = json.dumps(normalized, default=str)
                semantic_haystack = normalized_text if name == "long_thread" else f"{cleaned}\n{normalized_text}"
                missing = [fact for fact in expected_facts if fact.casefold() not in semantic_haystack.casefold()]
                if missing:
                    failures.append(f"{delta.email_id}: missing semantic facts {missing}")
                outputs.append({
                    "email_id": delta.email_id,
                    "strategy": delta.strategy,
                    "input_chars": delta.delta_length,
                    "required_markers": expected_markers,
                    "required_semantic_facts": expected_facts,
                    "missing_semantic_facts": missing,
                    "cleaned_chars": len(cleaned),
                    "normalization_type": type(normalized).__name__,
                })
            except Exception as exc:
                failures.append(f"{delta.email_id}: {self._sanitize_error(str(exc))}")

        if name == "duplicate" and len(non_empty) != 1:
            failures.append(f"expected 1 non-empty delta, got {len(non_empty)}")
        if name == "nested_reply":
            texts = [delta.text for delta in non_empty]
            if any("ALPHA_UNIQUE_731" in text for text in texts[1:]):
                failures.append("quoted ALPHA marker leaked into a reply delta")
        if name == "long_thread":
            combined_deltas = "\n".join(delta.text for delta in non_empty)
            if len(non_empty) != 100:
                failures.append(f"expected 100 non-empty deltas, got {len(non_empty)}")
            for index in range(100):
                if combined_deltas.count(f"Region-{index:03d}") != 1:
                    failures.append(f"Region-{index:03d} was not retained exactly once")
                    break
            for index in (20, 40, 60, 80):
                if deltas[index].strategy != "forward_reentry":
                    failures.append(f"forward wave {index // 20} did not use forward_reentry strategy")

        return {
            "name": name,
            "passed": not failures,
            "latency_ms": round((time.monotonic() - started) * 1000),
            "message_count": len(messages),
            "delta_count": len(non_empty),
            "results": outputs,
            "failures": failures,
        }

    @staticmethod
    def _legacy_unroll(text, ai_service):
        max_chars = 12000
        chunks = [text[index:index + max_chars] for index in range(0, len(text), max_chars)] or [""]
        cleaned_parts = []
        for chunk in chunks:
            result = ai_service.process_content(
                content=chunk,
                skill_name="email_unroll",
                source_type="email_pipeline_t4_test",
                metadata={
                    "chat_template_kwargs": {"enable_thinking": False},
                    "max_tokens": 768,
                    "request_timeout": 120,
                },
            )
            if isinstance(result, dict):
                cleaned = result.get("response") or result.get("text") or ""
            else:
                cleaned = str(result or "")
            if not cleaned.strip():
                raise ValueError("email_unroll returned empty output")
            cleaned_parts.append(cleaned)
        return "\n\n--- THREAD CONTINUATION ---\n\n".join(cleaned_parts)

    @staticmethod
    def _close_stale_test_audits():
        from datetime import timedelta
        from ai_orchestrator.models import AIAuditLog

        stale_before = django_timezone.now() - timedelta(minutes=15)
        stale = AIAuditLog.objects.filter(
            source_type="email_pipeline_t4_test",
            status="PROCESSING",
            created_at__lt=stale_before,
        )
        return stale.update(
            status="FAILED",
            is_success=False,
            completed_at=django_timezone.now(),
            error_message="Synthetic T4 test was interrupted or exceeded its execution window.",
        )

    @staticmethod
    def _markers(text):
        import re
        return re.findall(r"\b[A-Z]+(?:_[A-Z]+)*_\d{3}\b", text)

    @staticmethod
    def _expected_facts(case_name, email_id):
        if case_name == "long_thread" and email_id.startswith("long-wave-"):
            match = re.fullmatch(r"long-wave-(\d+)-batch-(\d+)", email_id)
            wave_index = int(match.group(1)) - 1
            batch_index = int(match.group(2)) - 1
            start = wave_index * 20 + batch_index * 5
            end = start + 4
            return (
                [f"Region-{index:03d}" for index in range(start, end + 1)]
                + [str(41 + start), str(41 + end), str(1200 + start * 17), str(1200 + end * 17)]
            )
        facts = {
            ("single", "single"): ["investment memorandum"],
            ("nested_reply", "original"): ["revenue", "125"],
            ("nested_reply", "reply"): ["customer concentration"],
            ("nested_reply", "nested"): ["approved", "diligence"],
            ("html_reply", "html-original"): ["previous terms"],
            ("html_reply", "html-reply"): ["revised valuation"],
            ("forward", "forward"): ["sector growth", "18"],
            ("duplicate", "duplicate-one"): ["FY26 revenue", "125", "customer concentration", "22"],
            ("long_body", "long"): ["beginning of long evidence", "end of long evidence"],
        }
        return facts.get((case_name, email_id), [])

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
