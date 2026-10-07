from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from ai_orchestrator.services.pipeline_registry import PipelineRegistryService
from ai_orchestrator.services.report_prompt_quality import upgrade_report_prompt
from ai_orchestrator.services.report_sections import ICReportSectionService, ReportSectionCitationError, ReportSectionTooShortError
from ai_orchestrator.services.report_sections import ReportSectionStructureError
from ai_orchestrator.services.report_financial_format import FINANCIAL_ROWS


class ReportQualityTests(SimpleTestCase):
    def test_next_steps_cites_current_task_triggers_without_changing_columns(self):
        updated = upgrade_report_prompt('Analyst', '## {{ section_title }}\n{{ content }}', section_title='Next Steps')
        self.assertIn('Next Steps evidence contract:', updated[1])
        self.assertIn('Keep the twelve existing columns', updated[1])
        self.assertIn('do not copy an uncited draft verbatim', updated[1])
        self.assertIn('Not assigned or To agree', updated[1])
        self.assertEqual(upgrade_report_prompt(*updated, section_title='Next Steps'), updated)

    def test_financial_final_contract_allows_additional_source_comparison_tables(self):
        updated = upgrade_report_prompt('Analyst', '## {{ section_title }}\n{{ content }}', section_title='Key Financials')
        self.assertIn('Financial output shape check:', updated[1])
        self.assertIn('Supplemental tables are allowed', updated[1])
        self.assertEqual(upgrade_report_prompt(*updated, section_title='Key Financials'), updated)

    def test_calculated_labels_and_supported_input_citations_do_not_force_a_retry(self):
        text='## Key Financials\n\n| Metric (INR Cr) | FY25 Actual |\n| --- | ---: |\n'
        values={name:'Not provided' for name in FINANCIAL_ROWS}
        values.update({'Revenue':'100 [1]','Cost of Goods Sold':'60 [2]','Gross Profit':'40',
            'Operating Expenses':'30 [3]','EBITDA':'10 [4]','Depreciation and Amortization':'2 [5]','EBIT':'8'})
        text+='\n'.join(f"| {name+' *(Calculated)*¹' if name in ['Gross Profit','EBIT'] else name} | {values[name]} |" for name in FINANCIAL_ROWS)
        normalized=ICReportSectionService._normalize_financial_metric_labels(text,'Key Financials')
        normalized=ICReportSectionService._cite_supported_financial_calculations(normalized,{1,2,3,4,5})
        self.assertIn('| Gross Profit [1] [2] | 40 |',normalized)
        self.assertIn('| EBIT [4] [5] | 8 |',normalized)
        ICReportSectionService._validate_financial_table(normalized,verified_citation_numbers={1,2,3,4,5})

    def test_unreconciled_derived_row_does_not_receive_borrowed_citations(self):
        text='| Revenue [1] | 100 |\n| Cost of Goods Sold [2] | 60 |\n| Gross Profit | 55 |'
        self.assertEqual(ICReportSectionService._cite_supported_financial_calculations(text,{1,2}),text)

    def test_source_values_use_saved_results_year_units_and_signed_profit(self):
        from ai_orchestrator.services.report_financial_format import financial_source_errors
        rows = [["Metric (INR Cr)", "FY27 Forecast"], ["---", "---"],
                *[[name, "Not provided"] for name in FINANCIAL_ROWS]]
        rows[2][1] = "10.00 [1]"
        rows[6][1] = "-2.50 [2]"
        citations = [{"citation_number": 1, "financial_cells": {"G10": {
            "value": 100, "row_label": "Total Revenue", "period": "FY27E",
            "unit_labels": ["INR Millions"], "number_format": "0.00"}}},
            {"citation_number": 2, "financial_cells": {"G26": {
            "value": -25, "row_label": "EBITDA", "period": "FY27E",
            "unit_labels": ["INR Millions"], "number_format": "0.00"}}}]
        self.assertEqual(financial_source_errors(rows, citations), [])
        rows[2][1] = "100.00 [1]"
        self.assertTrue(any("Revenue" in error for error in financial_source_errors(rows, citations)))
        rows[2][1] = "10.00 [1]"
        rows[6][1] = "2.50 [2]"
        self.assertTrue(any("EBITDA" in error for error in financial_source_errors(rows, citations)))

    def test_precise_cell_cannot_borrow_another_cells_value_or_capex_label(self):
        from ai_orchestrator.services.report_financial_format import financial_source_errors
        rows = [["Metric (INR Cr)", "FY27 Forecast"], ["---", "---"],
                *[[name, "Not provided"] for name in FINANCIAL_ROWS]]
        rows[7][1] = "0.11 [1]"
        source = {"citation_number": 1, "used_location": "CF!G25", "financial_cells": {
            "G25": {"value": -.11, "row_label": "Capex", "period": "FY27E", "unit_labels": ["INR Crores"]}}}
        self.assertTrue(any("different source metric" in error for error in financial_source_errors(rows, [source])))
        source["used_location"] = "IS!G28"
        source["financial_cells"] = {
            "F28": {"value": .11, "row_label": "Depreciation", "period": "FY26E", "unit_labels": ["INR Crores"]},
            "G28": {"value": .48, "row_label": "Depreciation", "period": "FY27E", "unit_labels": ["INR Crores"]}}
        self.assertTrue(any("G28=0.48" in error for error in financial_source_errors(rows, [source])))
        source["used_location"] = "IS!F28"
        self.assertTrue(any("different source period" in error for error in financial_source_errors(rows, [source])))

    def test_unknown_source_units_are_not_claimed_as_verified(self):
        from ai_orchestrator.services.report_financial_format import financial_source_errors
        rows = [["Metric (INR Cr)", "FY27 Forecast"], ["---", "---"],
                *[[name, "Not provided"] for name in FINANCIAL_ROWS]]
        rows[2][1] = "10 [1]"
        self.assertEqual(financial_source_errors(rows, [{"citation_number": 1, "financial_cells": {
            "G10": {"value": 100, "row_label": "Revenue", "period": "FY27E", "unit_labels": []}}}]), [])

    def test_financial_numeric_rows_require_verified_citations_inside_table(self):
        table = "| Metric (INR Cr) | FY25 Actual |\n| --- | ---: |\n"
        table += "\n".join(f"| {row} | {'100' if row == 'Revenue' else 'Not provided'} |" for row in FINANCIAL_ROWS)
        with self.assertRaises(ReportSectionCitationError):
            ICReportSectionService._validate_financial_table(table + "\n\nSource [1]", verified_citation_numbers={1})
        cited = table.replace("| 100 |", "| 100 [1] |")
        ICReportSectionService._validate_financial_table(cited, verified_citation_numbers={1})
        with self.assertRaises(ReportSectionCitationError):
            ICReportSectionService._validate_financial_table(cited, verified_citation_numbers={2})

    def test_financial_bridge_detects_other_income_counted_in_ebitda_twice(self):
        from ai_orchestrator.services.report_financial_format import financial_bridge_errors
        values = ["2670.37", "2519.33", "151.04", "134.44", "17.37", "0.91", "16.46", "0.06", "0.77", "Not provided", "16.40", "4.16", "12.24"]
        rows = [["Metric", "FY25 Actual"], ["---", "---"], *[[name, value + " [1]"] for name, value in zip(FINANCIAL_ROWS, values)]]
        self.assertTrue(any("EBITDA" in error for error in financial_bridge_errors(rows)))
        rows[6][1] = "16.60 [1]"
        rows[8][1] = "15.69 [1]"
        self.assertEqual(financial_bridge_errors(rows), [])

    def test_financial_bridge_allows_rounding_and_skips_missing_inputs(self):
        from ai_orchestrator.services.report_financial_format import financial_bridge_errors
        values = ["100", "33", "66", *["Not provided"] * 10]
        rows = [["Metric", "FY25 Actual"], ["---", "---"], *[[name,value] for name,value in zip(FINANCIAL_ROWS, values)]]
        self.assertEqual(financial_bridge_errors(rows), [])

    def test_authored_variable_heading_gets_financial_contract_by_stage(self):
        updated = upgrade_report_prompt("Analyst instructions", "## {{ section_title }}\n{{ content }}", section_title="Key Financials")
        self.assertIn("Key Financials table format:", updated[1])
        self.assertIn("one main standardized Markdown table", updated[0])
        self.assertEqual(upgrade_report_prompt(*updated, section_title="Key Financials"), updated)

    def test_single_income_statement_has_exact_revenue_to_pat_order(self):
        table = "| Metric (INR Cr) | FY25 Actual | FY26 Forecast |\n| --- | ---: | ---: |\n"
        table += "\n".join(f"| {row} | Not provided | Not provided |" for row in FINANCIAL_ROWS)
        ICReportSectionService._validate_financial_table("## Key Financials\n\n" + table)
        extra = '| Metric | FY25 |\n| --- | --- |\n| Revenue | 100 |\n| EBITDA | 10 |'
        ICReportSectionService._validate_financial_table(extra + '\n\n' + table + '\n\n' + extra)
        with self.assertRaises(ReportSectionStructureError):
            ICReportSectionService._validate_financial_table(extra)
        with self.assertRaises(ReportSectionStructureError):
            ICReportSectionService._validate_financial_table(table + "\n\n" + table)
        with self.assertRaises(ReportSectionStructureError):
            ICReportSectionService._validate_financial_table(table.replace("| PAT |", "| Cash |"))

    def test_supplemental_tables_do_not_bypass_main_statement_citations_or_math(self):
        extra = '| Metric | FY25 |\n| --- | --- |\n| Revenue [1] | 100 |'
        values = {name: 'Not provided' for name in FINANCIAL_ROWS}
        values.update({'Revenue': '100', 'Cost of Goods Sold': '60', 'Gross Profit': '55'})
        table = '| Metric | FY25 Actual |\n| --- | --- |\n' + '\n'.join(f'| {name} | {values[name]} |' for name in FINANCIAL_ROWS)
        with self.assertRaises(ReportSectionCitationError):
            ICReportSectionService._validate_financial_table(extra + '\n\n' + table, verified_citation_numbers={1})
        cited = table.replace('| 100 |', '| 100 [1] |').replace('| 60 |', '| 60 [1] |').replace('| 55 |', '| 55 [1] |')
        with self.assertRaises(ReportSectionStructureError):
            ICReportSectionService._validate_financial_table(extra + '\n\n' + cited, verified_citation_numbers={1})

    def test_cell_citation_cannot_target_an_unseen_cell_inside_a_broad_range(self):
        citation = {"locator": {"sheet_name": "PL", "row_start": 1, "row_end": 20,
            "column_start": "A", "column_end": "H"}, "visible_cells": ["A1", "B1", "C1"]}
        self.assertEqual(ICReportSectionService._validated_spreadsheet_location("PL!B1:C1", citation), "PL!B1:C1")
        self.assertEqual(ICReportSectionService._validated_spreadsheet_location("PL!F20", citation), "")

    def test_financial_prompt_owns_one_table_and_is_idempotent(self):
        updated = upgrade_report_prompt("Analyst instructions", "- Begin with the exact heading: ## Key Financials\n{{ content }}")
        self.assertIn("one main standardized Markdown table", updated[0])
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
        self.assertIn("Report reading hierarchy:", upgraded[1])
        self.assertIn("Bold one or two short", upgraded[1])
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
