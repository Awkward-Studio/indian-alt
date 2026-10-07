from django.test import SimpleTestCase
from ai_orchestrator.services.report_calculations import report_calculation_errors


class ReportCalculationTests(SimpleTestCase):
    def test_rounded_input_precision_allows_the_live_financial_ratios(self):
        self.assertEqual(report_calculation_errors('1.73 / 8.94 = 19.3%\n\n40.53 / 20.32 = 2.00x'), [])
        self.assertTrue(report_calculation_errors('1.73 / 8.94 = 21.3%\n\n40.53 / 20.32 = 2.20x'))

    def test_correct_ratios_and_irr_pass_with_display_rounding(self):
        self.assertEqual(report_calculation_errors('MOIC is 180 / 60 = 3.0x, and Gross IRR is approximately 24.6% over 5 years.'), [])

    def test_wrong_irr_and_division_are_rejected_without_deal_specific_rules(self):
        errors = report_calculation_errors('MOIC is 180 / 60 = 3.0x, and Gross IRR is approximately 40% over 5 years.\n\n300 / 150 = 4x.')
        self.assertEqual(len(errors), 2)

    def test_scenario_table_must_match_its_own_calculation(self):
        text='| Scenario | MOIC |\n| --- | --- |\n| Base case | 4.5x |\n\n#### Base case\nMOIC is 200 / 100 = 2.00x.'
        self.assertTrue(any('table MOIC' in error for error in report_calculation_errors(text)))

    def test_source_titles_and_symbolic_formulas_are_not_interpreted_as_calculations(self):
        self.assertEqual(report_calculation_errors('MOIC = proceeds / invested capital.\n\n### Citations\n1. Example 100 / 10 = 100x.'), [])
