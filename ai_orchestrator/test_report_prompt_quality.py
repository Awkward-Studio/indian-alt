from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from ai_orchestrator.services.pipeline_registry import PipelineRegistryService
from ai_orchestrator.services.report_prompt_quality import upgrade_report_prompt
from ai_orchestrator.services.report_sections import ICReportSectionService, ReportSectionCitationError, ReportSectionTooShortError
from ai_orchestrator.services.report_sections import ReportSectionStructureError
from ai_orchestrator.services.report_financial_format import FINANCIAL_ROWS


class ReportQualityTests(SimpleTestCase):
    def test_authored_variable_heading_gets_financial_contract_by_stage(self):
        updated = upgrade_report_prompt("Analyst instructions", "## {{ section_title }}\n{{ content }}", section_title="Key Financials")
        self.assertIn("Key Financials table format:", updated[1])
        self.assertIn("exactly ONE Markdown table", updated[0])
        self.assertEqual(upgrade_report_prompt(*updated, section_title="Key Financials"), updated)

    def test_single_income_statement_has_exact_revenue_to_pat_order(self):
        table = "| Metric (INR Cr) | FY25 Actual | FY26 Forecast |\n| --- | ---: | ---: |\n"
        table += "\n".join(f"| {row} | Not provided | Not provided |" for row in FINANCIAL_ROWS)
        ICReportSectionService._validate_financial_table("## Key Financials\n\n" + table)
        with self.assertRaises(ReportSectionStructureError):
            ICReportSectionService._validate_financial_table(table + "\n\n" + table)
        with self.assertRaises(ReportSectionStructureError):
            ICReportSectionService._validate_financial_table(table.replace("| PAT |", "| Cash |"))

    def test_cell_citation_cannot_target_an_unseen_cell_inside_a_broad_range(self):
        citation = {"locator": {"sheet_name": "PL", "row_start": 1, "row_end": 20,
            "column_start": "A", "column_end": "H"}, "visible_cells": ["A1", "B1", "C1"]}
        self.assertEqual(ICReportSectionService._validated_spreadsheet_location("PL!B1:C1", citation), "PL!B1:C1")
        self.assertEqual(ICReportSectionService._validated_spreadsheet_location("PL!F20", citation), "")

    def test_financial_prompt_owns_one_table_and_is_idempotent(self):
        updated = upgrade_report_prompt("Analyst instructions", "- Begin with the exact heading: ## Key Financials\n{{ content }}")
        self.assertIn("exactly ONE Markdown table", updated[0])
        self.assertIn("Key Financials table format:", updated[1])
        self.assertEqual(upgrade_report_prompt(*updated), updated)
    def test_verified_source_wrappers_render_cleanly_in_tables(self):
        text = "| Revenue | 100 [IM: Model.xlsx @R001, R002] |\n| Profit | 10 [EXT: R002] |"
        citation = {"document_id": "doc", "title": "Model.xlsx", "location": "PL!A1:B2"}
        rendered, used = ICReportSectionService._replace_internal_citations(text, {"1": citation, "2": citation})
        self.assertNotIn("IM:", rendered)
        self.assertIn("100 [1]", rendered)
        self.assertIn("External evidence: [1]", rendered)
        self.assertEqual(len(used), 1)
        self.assertEqual(len(rendered.splitlines()), 2)

    def test_filename_only_labels_remain_rejected_even_with_one_valid_marker(self):
        with self.assertRaises(ReportSectionCitationError):
            ICReportSectionService._normalize_section("Company Details",
                "## Company Details\n\nGrowth is reported [R001]. A different claim [IM: Memo.pdf].",
                citations={"1": {"document_id": "doc", "title": "Memo.pdf"}})

    def test_reference_list_does_not_make_a_short_section_pass(self):
        source = {"document_id": "doc", "title": "A very long document title " * 100}
        with self.assertRaises(ReportSectionTooShortError):
            ICReportSectionService._normalize_section("Company Details",
                "## Company Details\n\nThe source supports a short finding but insufficient analysis [R001].",
                citations={"1": source}, minimum_words=50)

    def test_upgrade_preserves_business_instructions_variables_and_is_idempotent(self):
        system = "Be skeptical, commercially practical and concise.\nTag every factual claim and numeric table cell [IM: supplied source]."
        user = "- Assess receivables and sensitivity.\n{{ content }}\n{{ model_data_json }}"
        upgraded = upgrade_report_prompt(system, user)
        self.assertIn("Assess receivables and sensitivity.", upgraded[1])
        self.assertIn("{{ content }}", upgraded[1])
        self.assertIn("{{ model_data_json }}", upgraded[1])
        self.assertIn("not abbreviated coverage", upgraded[0])
        self.assertEqual(upgrade_report_prompt(*upgraded), upgraded)


class ReportPromptPublicationTests(TestCase):
    def test_command_creates_reversible_revisions_and_preserves_schemas(self):
        PipelineRegistryService.ensure_report_pipeline_defaults()
        old = PipelineRegistryService.resolve_stage("ic_report_generation", "key_financials").prompt_revision
        call_command("upgrade_ic_report_quality", verbosity=0)
        self.assertEqual(PipelineRegistryService.resolve_stage("ic_report_generation", "key_financials").prompt_revision.id, old.id)
        call_command("upgrade_ic_report_quality", apply=True, verbosity=0)
        new = PipelineRegistryService.resolve_stage("ic_report_generation", "key_financials").prompt_revision
        self.assertNotEqual(new.id, old.id)
        self.assertEqual(new.input_schema, old.input_schema)
        self.assertEqual(new.output_schema, old.output_schema)
        old.refresh_from_db()
        self.assertEqual(old.status, "archived")
