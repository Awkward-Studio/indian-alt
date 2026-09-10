from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from ai_orchestrator.prompt_contracts import IC_REPORT_HEADERS, IC_SECTION_TITLES
from ai_orchestrator.services.report_sections import ICReportSectionService


class ICReportSectionServiceTests(SimpleTestCase):
    def complete_report(self):
        return "\n\n".join(f"{header}\n\nComplete evidence-backed content for {header}." for header in IC_REPORT_HEADERS)

    def test_complete_report_does_not_call_model(self):
        service = Mock()

        result = ICReportSectionService.complete(
            ai_service=service,
            report=self.complete_report(),
            evidence="Evidence",
            analysis={"deal_model_data": {}},
            source_id="email-1",
        )

        self.assertTrue(ICReportSectionService.is_complete(result))
        service.process_content.assert_not_called()

    @override_settings(
        EMAIL_REPORT_SECTION_MAX_TOKENS=3072,
        EMAIL_REPORT_SECTION_TIMEOUT=1800,
    )
    @patch("ai_orchestrator.services.report_sections.cache")
    def test_incomplete_tail_is_generated_and_cached_by_section(self, report_cache):
        report_cache.get.return_value = None
        service = Mock()
        service.process_content.side_effect = lambda **kwargs: {
            "response": f"## {kwargs['content'].split('Required heading: ## ', 1)[1].splitlines()[0]}\n\nGenerated section with enough evidence-backed detail to pass validation."
        }
        partial = "\n\n".join(
            f"{header}\n\nExisting complete section with enough detail."
            for header in IC_REPORT_HEADERS[:8]
        )

        result = ICReportSectionService.complete(
            ai_service=service,
            report=partial,
            evidence="Private evidence",
            analysis={"deal_model_data": {"title": "Example"}},
            source_id="email-1",
        )

        self.assertEqual(ICReportSectionService.headings(result), list(IC_SECTION_TITLES))
        self.assertEqual(service.process_content.call_count, 4)
        self.assertTrue(all(call.kwargs["metadata"]["max_tokens"] == 3072 for call in service.process_content.call_args_list))
        self.assertTrue(all(call.kwargs["metadata"]["request_timeout"] == 1800 for call in service.process_content.call_args_list))
        self.assertEqual(report_cache.set.call_count, 4)

    @patch("ai_orchestrator.services.report_sections.cache")
    def test_cached_section_skips_model_call(self, report_cache):
        report_cache.get.return_value = "## Executive Summary\n\nCached complete section content for the report."
        service = Mock()

        section = ICReportSectionService._generate_section(
            ai_service=service,
            evidence="Evidence",
            analysis={"deal_model_data": {}},
            title="Executive Summary",
            source_id="email-1",
        )

        self.assertIn("Cached complete section", section)
        service.process_content.assert_not_called()

    @patch("ai_orchestrator.services.report_sections.cache")
    def test_each_section_can_receive_distinct_retrieved_evidence(self, report_cache):
        report_cache.get.return_value = None
        service = Mock()
        service.process_content.side_effect = lambda **kwargs: {
            "response": f"## {kwargs['content'].split('Required heading: ## ', 1)[1].splitlines()[0]}\n\nGenerated section with complete evidence-backed detail."
        }

        result = ICReportSectionService.complete(
            ai_service=service,
            report="",
            evidence="",
            evidence_for_section=lambda title: {
                "context": f"Only evidence ranked for {title}",
                "metadata": {"selected_chunk_count": 25},
            },
            analysis={"deal_model_data": {"title": "Example"}},
            source_id="report-1",
            source_type="vdr_report_section",
            context_label_prefix="VDR report section",
            max_tokens=16_384,
            max_input_tokens=40_960,
        )

        self.assertTrue(ICReportSectionService.is_complete(result))
        self.assertEqual(service.process_content.call_count, len(IC_SECTION_TITLES))
        for title, call in zip(IC_SECTION_TITLES, service.process_content.call_args_list):
            self.assertIn(f"Only evidence ranked for {title}", call.kwargs["content"])
            self.assertEqual(call.kwargs["source_type"], "vdr_report_section")
            self.assertEqual(call.kwargs["metadata"]["max_tokens"], 16_384)
            self.assertEqual(call.kwargs["metadata"]["max_input_tokens"], 40_960)
