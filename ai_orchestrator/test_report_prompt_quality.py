from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from ai_orchestrator.services.pipeline_registry import PipelineRegistryService
from ai_orchestrator.services.report_prompt_quality import upgrade_report_prompt
from ai_orchestrator.services.report_sections import ICReportSectionService, ReportSectionCitationError, ReportSectionTooShortError
from ai_orchestrator.services.report_sections import ReportSectionStructureError
from ai_orchestrator.services.report_financial_format import FINANCIAL_ROWS


class ReportQualityTests(SimpleTestCase):
    def test_citation_support_is_non_blocking_but_known_wrong_source_values_still_fail(self):
        from ai_orchestrator.services.report_financial_format import financial_source_errors
        rows=[['Metric (INR Cr)','FY28 Forecast'],['---','---'],*[[name,'215.44 [1]' if name=='EBITDA' else 'Not provided'] for name in FINANCIAL_ROWS]]
        sources=[{'citation_number':1,'title':'Model.xlsx','financial_cells':{}}]
        self.assertEqual(financial_source_errors(rows,sources,check_citation_support=False), [])
        sources[0]['financial_cells']={'G8':{'value':'2154373474.2875','row_label':'EBITDA','period':'FY28E','unit_labels':['INR']}}
        rows[6][1]='225.44 [1]'
        self.assertTrue(financial_source_errors(rows,sources,check_citation_support=False))

    def test_prepared_conversions_preserve_sign_precision_and_declared_units(self):
        from ai_orchestrator.services.report_financial_format import prepared_display_values
        fact = {'value': '-122746186', 'row_label': 'Total Revenue', 'unit_labels': ['Revenue (₹)'], 'number_format': '0.00'}
        values = prepared_display_values(fact)
        self.assertEqual(values['INR Cr'], {'exact': '-12.2746186', 'display_2dp': '-12.27'})
        self.assertEqual(values['INR million']['exact'], '-122.746186')
        for changed in [{'unit_labels': []}, {'unit_labels': ['INR Cr', 'USD million']},
                        {'unit_labels': ['INR', 'INR Cr']}, {'number_format': '0.00%'},
                        {'row_label': 'Customer count'}, {'value': 'NaN'}]:
            self.assertEqual(prepared_display_values({**fact, **changed}), {})

    def test_forecast_ranges_keep_both_bounds_and_qualifiers_without_changing_amounts(self):
        table = '| Metric | FY25 Actual | FY26 Forecast [2] |\n| --- | --- | --- |\n' + '\n'.join(f'| {name} | Not provided | Not provided |' for name in FINANCIAL_ROWS)
        table = table.replace('| Revenue | Not provided | Not provided |', '| Revenue [1] | 115.00 | 132.25 – 138.00 [2] |').replace('| EBITDA | Not provided | Not provided |', '| EBITDA [1] | 16.79 (BU) | 26.45 – 28.98 (Target) [2] |')
        normalized = ICReportSectionService._normalize_financial_amount_cells(table)
        self.assertIn('FY26 Forecast [2] (Lower bound) | FY26 Forecast [2] (Upper bound)', normalized)
        self.assertIn('| Revenue [1] | 115.00 | 132.25 [2] | 138.00 [2] |', normalized)
        self.assertIn('| EBITDA [1] | 16.79 | 26.45 [2] | 28.98 [2] |', normalized)
        self.assertIn('FY25 Actual: BU.* [1]', normalized)
        self.assertIn('FY26 Forecast: Target.* [2]', normalized)
        ICReportSectionService._validate_financial_table(normalized, verified_citation_numbers={1, 2})
        self.assertEqual(ICReportSectionService._normalize_financial_amount_cells(normalized), normalized)
        unsupported = table.replace('115.00', 'approximately 115.00')
        self.assertIn('approximately 115.00', ICReportSectionService._normalize_financial_amount_cells(unsupported))

    def test_main_statement_period_sources_are_repeated_in_numeric_cells(self):
        table = '| Metric | FY25 Actual [1] | FY26 Forecast [2] |\n| --- | --- | --- |\n' + '\n'.join(f'| {name} | {"100" if name == "Revenue" else "Not provided"} | Not provided |' for name in FINANCIAL_ROWS)
        expanded = ICReportSectionService._expand_financial_period_citations(table, {1, 2})
        self.assertIn('| Revenue | 100 [1] | Not provided |', expanded)
        ICReportSectionService._validate_financial_table(expanded, verified_citation_numbers={1, 2})
        self.assertEqual(ICReportSectionService._expand_financial_period_citations(expanded, {1, 2}), expanded)
        self.assertEqual(ICReportSectionService._expand_financial_period_citations(table, {2}), table)
        explicit = table.replace('| 100 |', '| 100 [3] |')
        self.assertEqual(ICReportSectionService._expand_financial_period_citations(explicit, {1, 2, 3}), explicit)
        extra = '| Metric | FY25 Actual [1] |\n| --- | --- |\n| Revenue | 200 |'
        self.assertEqual(ICReportSectionService._expand_financial_period_citations(extra, {1}), extra)

    def test_cost_classification_labels_preserve_notes_and_values_without_schema_retry(self):
        table = '| Metric | FY25 Actual |\n| --- | --- |\n' + '\n'.join(f'| {name} | Not provided |' for name in FINANCIAL_ROWS)
        table = table.replace('| Cost of Goods Sold |', '| Cost of Goods Sold (Purchased services) [1] |').replace('| Operating Expenses |', '| Operating Expenses (Personnel + administration) [2] |')
        normalized = ICReportSectionService._normalize_financial_metric_labels(table, 'Key Financials')
        self.assertIn('| Cost of Goods Sold [1] | Not provided |', normalized)
        self.assertIn('| Operating Expenses [2] | Not provided |', normalized)
        self.assertIn('*Cost of Goods Sold classification: purchased services.* [1]', normalized)
        self.assertIn('*Operating Expenses classification: personnel + administration.* [2]', normalized)
        ICReportSectionService._validate_financial_table(normalized)
        self.assertEqual(ICReportSectionService._normalize_financial_metric_labels(normalized, 'Key Financials'), normalized)
        adjusted = table.replace('Purchased services', 'Adjusted excluding overhead')
        self.assertIn('Adjusted excluding overhead', ICReportSectionService._normalize_financial_metric_labels(adjusted, 'Key Financials'))

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

    def test_financial_numeric_rows_do_not_require_citation_validation(self):
        table = "| Metric (INR Cr) | FY25 Actual |\n| --- | ---: |\n"
        table += "\n".join(f"| {row} | {'100' if row == 'Revenue' else 'Not provided'} |" for row in FINANCIAL_ROWS)
        ICReportSectionService._validate_financial_table(table + "\n\nSource [1]", verified_citation_numbers={1})
        cited = table.replace("| 100 |", "| 100 [1] |")
        ICReportSectionService._validate_financial_table(cited, verified_citation_numbers={1})
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

    def test_supplemental_tables_do_not_bypass_main_statement_math(self):
        extra = '| Metric | FY25 |\n| --- | --- |\n| Revenue [1] | 100 |'
        values = {name: 'Not provided' for name in FINANCIAL_ROWS}
        values.update({'Revenue': '100', 'Cost of Goods Sold': '60', 'Gross Profit': '55'})
        table = '| Metric | FY25 Actual |\n| --- | --- |\n' + '\n'.join(f'| {name} | {values[name]} |' for name in FINANCIAL_ROWS)
        with self.assertRaises(ReportSectionStructureError):
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

    def test_filename_only_labels_are_non_blocking_with_a_valid_marker(self):
        ICReportSectionService._normalize_section("Company Details",
            "## Company Details\n\nGrowth is reported [R001]. A different claim [IM: Memo.pdf].",
            citations={"1": {"document_id": "doc", "title": "Memo.pdf"}})
    def test_short_cited_section_is_not_rejected_for_word_count(self):
        source = {"document_id": "doc", "title": "A very long document title " * 100}
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
