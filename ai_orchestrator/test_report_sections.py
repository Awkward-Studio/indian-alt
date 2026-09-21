from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TestCase, override_settings

from ai_orchestrator.prompt_contracts import IC_REPORT_HEADERS, IC_SECTION_TITLES
from ai_orchestrator.services.pipeline_registry import PipelineRegistryService
from ai_orchestrator.services.report_sections import (
    ICReportSectionService,
    ReportSectionValidationError,
)


class CitationNormalizationTests(SimpleTestCase):
    def test_runaway_unclosed_citation_cluster_fails_without_regex_backtracking(self):
        raw = (
            "## Industry Overview\n\n"
            "The market is fragmented [R001, "
            + ", ".join(["R010"] * 4_000)
        )

        with self.assertRaisesRegex(
            ReportSectionValidationError,
            "degenerate repeated citation output",
        ):
            ICReportSectionService._normalize_section(
                "Industry Overview",
                raw,
                citations={
                    "1": {"document_id": "doc-1", "title": "Memo.pdf"},
                    "10": {"document_id": "doc-2", "title": "Market.pdf"},
                },
            )

    def test_repeated_ranks_for_the_same_document_render_once_per_cluster(self):
        citation = {
            "document_id": "email-1",
            "title": "Longway investment email",
            "reference": "Longway investment email",
        }
        rendered, used = ICReportSectionService._replace_internal_citations(
            "Revenue is INR 175 crore [R001], [R002], [R001].",
            {"1": citation, "2": citation},
        )

        self.assertEqual(len(used), 1)
        self.assertEqual(rendered, "Revenue is INR 175 crore [1].")

    def test_unknown_rank_is_removed_without_discarding_verified_cluster_citation(self):
        citation = {
            "document_id": "doc-1",
            "title": "Investment memo.pdf",
            "url": "https://contoso.example/investment-memo.pdf",
            "reference": "[Investment memo.pdf](<https://contoso.example/investment-memo.pdf>)",
        }

        rendered = ICReportSectionService._normalize_section(
            "Company Details",
            (
                "## Company Details\n\n"
                "The company operates a scaled sourcing platform with verified enterprise demand "
                "and a growing customer base [R001, R060]."
            ),
            citations={"1": citation},
        )

        body, citations = rendered.split("### Citations", 1)
        self.assertIn("customer base [1].", body)
        self.assertNotIn("Investment memo.pdf", body)
        self.assertIn("1. [Investment memo.pdf](<https://contoso.example/investment-memo.pdf>)", citations)
        self.assertNotIn("R060", rendered)
        self.assertNotIn("[,", rendered)

    def test_spreadsheet_row_in_citation_footer_is_not_an_internal_rank(self):
        rendered = ICReportSectionService._normalize_section(
            "Key Financials",
            "## Key Financials\n\nRevenue increased and EBITDA improved [R001].",
            citations={
                "1": {
                    "document_id": "doc-1",
                    "title": "Financial Model.xlsx",
                    "url": "https://contoso.sharepoint.com/financial-model.xlsx",
                    "location": "Overall PL!R60",
                    "locator": {
                        "sheet_name": "Overall PL",
                        "row_start": 60,
                        "row_end": 60,
                        "column_start": "R",
                        "column_end": "R",
                    },
                }
            },
        )

        body, citations = rendered.split("### Citations", 1)
        self.assertIn("EBITDA improved [1].", body)
        self.assertIn("Overall PL!R60", citations)

    def test_citation_cleanup_preserves_markdown_table_row_boundaries(self):
        rendered = ICReportSectionService._normalize_section(
            "Key Financials",
            (
                "## Key Financials\n\n"
                "| Metric | FY24A | FY25A |\n"
                "| --- | ---: | ---: |\n"
                "| Revenue | 100 [R001] | 125 [R001] |\n"
                "| EBITDA | 10 [R001] | 15 [R001] |"
            ),
            citations={"1": {"document_id": "doc-1", "title": "Model.xlsx"}},
        )

        body = rendered.split("### Citations", 1)[0]
        table_lines = [line for line in body.splitlines() if line.startswith("|")]
        self.assertEqual(len(table_lines), 4)
        self.assertEqual(table_lines[1], "| --- | ---: | ---: |")

    def test_key_financials_transposes_period_rows_to_metric_rows(self):
        rendered = ICReportSectionService._normalize_section(
            "Key Financials",
            (
                "## Key Financials\n\n"
                "| Period | Revenue | EBITDA | PAT |\n"
                "| --- | ---: | ---: | ---: |\n"
                "| FY24A | 100 | 10 | 4 |\n"
                "| FY25A | 125 | 15 | 6 |"
            ),
        )

        self.assertIn("| Metric | FY24A | FY25A |", rendered)
        self.assertIn("| Revenue | 100 | 125 |", rendered)
        self.assertIn("| EBITDA | 10 | 15 |", rendered)
        self.assertNotIn("| Period | Revenue | EBITDA |", rendered)

    def test_section_with_only_unknown_ranks_fails_as_non_retryable_validation(self):
        with self.assertRaises(ReportSectionValidationError):
            ICReportSectionService._normalize_section(
                "Company Details",
                (
                    "## Company Details\n\n"
                    "The company operates a scaled sourcing platform with enterprise demand [R060]."
                ),
                citations={"1": {"document_id": "doc-1", "title": "Memo.pdf"}},
            )


class ICReportSectionServiceTests(TestCase):
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
                f"## {kwargs['metadata']['section_title']}"
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
            "response": f"## {kwargs['metadata']['section_title']}\n\nGenerated section with enough evidence-backed detail to pass validation."
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

    @patch("ai_orchestrator.services.report_sections.cache")
    def test_published_prompt_revision_invalidates_section_cache(self, report_cache):
        report_cache.get.return_value = None
        PipelineRegistryService.ensure_report_pipeline_defaults()
        resolved = PipelineRegistryService.resolve_stage(
            "ic_report_generation", "executive_summary"
        )
        service = Mock()
        service.process_content.return_value = {
            "response": "## Executive Summary\n\nEvidence-backed content for the investment committee."
        }

        ICReportSectionService._generate_section(
            ai_service=service,
            evidence="Same evidence",
            analysis={"deal_model_data": {}},
            title="Executive Summary",
            source_id="report-1",
        )
        first_key = report_cache.get.call_args.args[0]

        edited = PipelineRegistryService.create_prompt_draft(
            resolved.stage.prompt_definition,
            user_template=resolved.prompt_revision.user_template.replace(
                "Write exactly one section", "Write one revised section"
            ),
            system_template=resolved.prompt_revision.system_template,
        )
        PipelineRegistryService.publish_prompt(edited)
        ICReportSectionService._generate_section(
            ai_service=service,
            evidence="Same evidence",
            analysis={"deal_model_data": {}},
            title="Executive Summary",
            source_id="report-1",
        )
        second_key = report_cache.get.call_args.args[0]

        self.assertNotEqual(first_key, second_key)
        self.assertEqual(
            service.process_content.call_args.kwargs["metadata"]["_source_metadata"]["prompt_revision"],
            f"{edited.id}:r{edited.revision}",
        )

    @override_settings(VDR_REPORT_SECTION_MIN_WORDS=0)
    @patch("ai_orchestrator.services.report_sections.cache")
    def test_each_section_can_receive_distinct_retrieved_evidence(self, report_cache):
        report_cache.get.return_value = None
        service = Mock()
        service.process_content.side_effect = lambda **kwargs: {
            "response": f"## {kwargs['metadata']['section_title']}\n\nGenerated section with complete evidence-backed detail."
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
            self.assertEqual(call.kwargs["metadata"]["pipeline_key"], "ic_report_generation")
            self.assertEqual(call.kwargs["metadata"]["section_title"], title)
            self.assertTrue(call.kwargs["metadata"]["_source_metadata"]["prompt_revision"])

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
        body, citations = section.split("### Citations", 1)
        self.assertIn("Revenue was INR 100 crore [1].", body)
        self.assertNotIn("Investment Memorandum", body)
        self.assertIn(f"1. [Investment Memorandum 2025.pdf, p. 7](<{source_url}>)", citations)
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
        self.assertEqual(section.count("### Citations"), 1)
        self.assertEqual(section.count("### References"), 0)
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

        body, citations = section.split("### Citations", 1)
        self.assertIn("Revenue increased [1].", body)
        self.assertNotIn("Project Fit - FM.xlsx", body)
        self.assertIn("Project Fit - FM.xlsx, Revenue Build!F42:H42", citations)
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
