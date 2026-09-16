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

    @patch("ai_orchestrator.services.report_sections.cache")
    def test_force_regenerate_rebuilds_an_existing_complete_report(self, report_cache):
        report_cache.get.return_value = "## Executive Summary\n\nOld cached section."
        service = Mock()
        service.process_content.side_effect = lambda **kwargs: {
            "response": (
                f"## {kwargs['content'].split('Required heading: ## ', 1)[1].splitlines()[0]}"
                "\n\nFresh report section with enough evidence-backed detail."
            )
        }

        result = ICReportSectionService.complete(
            ai_service=service,
            report=self.complete_report(),
            evidence="Unchanged deal evidence",
            analysis={"deal_model_data": {}},
            source_id="report-1",
            force_regenerate=True,
        )

        self.assertTrue(ICReportSectionService.is_complete(result))
        self.assertEqual(service.process_content.call_count, len(IC_SECTION_TITLES))
        report_cache.get.assert_not_called()

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
    def test_force_regenerate_bypasses_unchanged_section_cache(self, report_cache):
        report_cache.get.return_value = "## Executive Summary\n\nPreviously cached report section."
        service = Mock()
        service.process_content.return_value = {
            "response": "## Executive Summary\n\nFreshly generated report section with source-backed analysis."
        }

        section = ICReportSectionService._generate_section(
            ai_service=service,
            evidence="Unchanged evidence",
            analysis={"deal_model_data": {}},
            title="Executive Summary",
            source_id="report-1",
            force_regenerate=True,
        )

        self.assertIn("Freshly generated", section)
        report_cache.get.assert_not_called()
        service.process_content.assert_called_once()
        self.assertTrue(
            service.process_content.call_args.kwargs["metadata"]["_source_metadata"]["force_regenerate"]
        )

    @override_settings(VDR_REPORT_SECTION_MIN_WORDS=0)
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
            self.assertIn("Cite every material factual statement", call.kwargs["content"])
            self.assertIn("Do not write a References section", call.kwargs["content"])

    def test_internal_evidence_reference_becomes_linked_precise_citation(self):
        source_url = "https://contoso.sharepoint.com/document?id=123"
        citation = {
            "rank": 20,
            "document_id": "doc-1",
            "title": "Investment Memorandum 2025.pdf",
            "url": source_url,
            "location": "p. 7",
            "locator": {"page": 7},
            "reference": f"[Investment Memorandum 2025.pdf](<{source_url}>)",
        }

        section = ICReportSectionService._normalize_section(
            "Executive Summary",
            "## Executive Summary\n\nRevenue was INR 100 crore [Evidence 20].",
            citations={"20": citation},
        )

        self.assertNotIn("Evidence 20", section)
        self.assertIn(
            f"[Investment Memorandum 2025.pdf, p. 7](<{source_url}>)",
            section,
        )
        self.assertIn("### References", section)
        self.assertIn(citation["reference"], section)
        self.assertIn("cited at p. 7", section)

    def test_model_references_are_removed_before_marker_expansion(self):
        source_url = "https://contoso.sharepoint.com/model.xlsx"
        citation = {
            "rank": 1,
            "document_id": "doc-1",
            "title": "Model.xlsx",
            "url": source_url,
            "location": "Revenue!A1:H8",
            "locator": {
                "sheet_name": "Revenue",
                "row_start": 1,
                "row_end": 8,
                "column_start": "A",
                "column_end": "H",
            },
            "reference": f"[Model.xlsx](<{source_url}>)",
        }
        raw = (
            "## Executive Summary\n\nRevenue increased [R001].\n\n"
            "### References\n\n"
            "- [Model.xlsx](https://example.com) (Used for: R001, R001, R001)"
        )

        section = ICReportSectionService._normalize_section(
            "Executive Summary",
            raw,
            citations={"1": citation},
        )

        self.assertNotIn("example.com", section)
        self.assertNotIn("Used for", section)
        self.assertEqual(section.count("### References"), 1)
        self.assertEqual(section.count("cited at Revenue!A1:H8"), 1)

    def test_spreadsheet_subrange_is_validated_and_rendered(self):
        source_url = "https://contoso.sharepoint.com/model.xlsx"
        citation = {
            "rank": 42,
            "document_id": "doc-1",
            "title": "Project Fit - FM.xlsx",
            "url": source_url,
            "location": "Revenue Build!A42:H49",
            "locator": {
                "sheet_name": "Revenue Build",
                "row_start": 42,
                "row_end": 49,
                "column_start": "A",
                "column_end": "H",
            },
            "reference": f"[Project Fit - FM.xlsx](<{source_url}>)",
        }

        section = ICReportSectionService._normalize_section(
            "Key Financials",
            "## Key Financials\n\nRevenue increased [R042@'Revenue Build'!F42:H42].",
            citations={"42": citation},
        )

        self.assertIn("Project Fit - FM.xlsx, Revenue Build!F42:H42", section)
        self.assertIn("cited at Revenue Build!F42:H42", section)
        self.assertIn("activeCell=%27Revenue+Build%27%21F42", section)

    def test_out_of_bounds_spreadsheet_subrange_falls_back_to_verified_chunk(self):
        citation = {
            "rank": 1,
            "document_id": "doc-1",
            "title": "Model.xlsx",
            "url": "",
            "location": "Revenue!A1:H8",
            "locator": {
                "sheet_name": "Revenue",
                "row_start": 1,
                "row_end": 8,
                "column_start": "A",
                "column_end": "H",
            },
            "reference": "Model.xlsx",
        }

        section = ICReportSectionService._normalize_section(
            "Executive Summary",
            "## Executive Summary\n\nRevenue increased [R001@'Revenue'!Z99:Z99].",
            citations={"1": citation},
        )

        self.assertIn("Model.xlsx, Revenue!A1:H8", section)
        self.assertNotIn("Z99", section)

    def test_unresolved_internal_reference_fails_validation(self):
        with self.assertRaisesRegex(ValueError, "unresolved internal citation"):
            ICReportSectionService._normalize_section(
                "Executive Summary",
                "## Executive Summary\n\nRevenue was INR 100 crore [Evidence 999].",
                citations={},
            )

    def test_section_with_ranked_evidence_requires_a_verifiable_marker(self):
        with self.assertRaisesRegex(ValueError, "no verifiable evidence citations"):
            ICReportSectionService._normalize_section(
                "Executive Summary",
                "## Executive Summary\n\nRevenue was INR 100 crore without a source marker.",
                citations={
                    "1": {
                        "document_id": "doc-1",
                        "title": "Model.xlsx",
                        "url": "https://contoso.sharepoint.com/model.xlsx",
                    }
                },
            )

    def test_unverified_model_link_fails_validation(self):
        with self.assertRaisesRegex(ValueError, "unverified source link"):
            ICReportSectionService._normalize_section(
                "Executive Summary",
                (
                    "## Executive Summary\n\nRevenue was INR 100 crore [R001]. "
                    "[Unsupported](https://example.com/source)."
                ),
                citations={
                    "1": {
                        "document_id": "doc-1",
                        "title": "Model.xlsx",
                        "url": "https://contoso.sharepoint.com/model.xlsx",
                        "location": "Revenue!A1:H8",
                        "reference": "[Model.xlsx](<https://contoso.sharepoint.com/model.xlsx>)",
                    }
                },
            )
