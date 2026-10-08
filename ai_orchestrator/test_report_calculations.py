from django.test import SimpleTestCase
from ai_orchestrator.services.report_calculations import report_calculation_errors


class ReportCalculationTests(SimpleTestCase):
    def test_percentage_notation_is_normalized_before_arithmetic(self):
        self.assertEqual(report_calculation_errors(r'Margin = $47.90 / 15.52 = 308.63\%$.'), [])
        self.assertEqual(report_calculation_errors(r'Margin = $3,022.02 / 6,957.26 = 43.44\%$.'), [])
        self.assertTrue(report_calculation_errors(r'Margin = $100 / 200 = 90\%$.' ))
        self.assertTrue(report_calculation_errors('Margin = 100 / 200 = 90 percent.'))
    def test_compound_equations_do_not_validate_only_the_trailing_division(self):
        self.assertEqual(report_calculation_errors('Adjusted leverage = 33.39 * 5.62 / 161.31 = 1.16x.'), [])
        self.assertTrue(report_calculation_errors('Adjusted leverage = 33.39 * 5.62 / 161.31 = 2.16x.'))
        self.assertTrue(report_calculation_errors('Simple ratio = 5.62 / 161.31 = 1.16x.'))
        self.assertEqual(report_calculation_errors('Proceeds = 100 * 5 / 10 = 50.0.'), [])
        self.assertTrue(report_calculation_errors('Proceeds = 100 * 5 / 0 = 50.0.'))
    def test_small_percentage_error_is_corrected_with_the_calculator(self):
        from ai_orchestrator.services.report_calculations import correct_small_percentage_calculations
        text,corrections=correct_small_percentage_calculations('Margin = 12.42 / 15.52 = 79.9%.')
        self.assertIn('= 80.0%',text)
        self.assertEqual(len(corrections),1)
        self.assertEqual(report_calculation_errors(text),[])
        self.assertEqual(correct_small_percentage_calculations('Margin = 12.42 / 15.52 = 70.0%.')[1],[])
        self.assertEqual(correct_small_percentage_calculations('> Source: 12.42 / 15.52 = 79.9%.')[1],[])
        references = 'Analysis.\n\n### Citations\n\n1. Source title: 12.42 / 15.52 = 79.9%.\n'
        self.assertEqual(correct_small_percentage_calculations(references), (references, []))

    def test_section_records_correction_and_saves_larger_errors_with_a_visible_flag(self):
        from ai_orchestrator.services.report_sections import ICReportSectionService, ReportSectionStructureError
        corrections = []
        section = ICReportSectionService._normalize_section(
            'Key Financials', 'Calculated margin for the displayed amounts is 12.42 / 15.52 = 79.9%.',
            calculation_corrections=corrections,
        )
        self.assertIn('12.42 / 15.52 = 80.0%', section)
        self.assertEqual(corrections[0]['basis'], 'displayed operands')
        warnings = []
        flagged = ICReportSectionService._normalize_section(
            'Key Financials', 'Calculated margin for the displayed amounts is 12.42 / 15.52 = 70.0%.',
            validation_warnings=warnings,
        )
        self.assertIn('### Source gaps and calculation issues', flagged)
        self.assertIn('Number conflict', flagged)
        self.assertTrue(warnings)
    def test_percentage_stake_keeps_the_full_exit_proceeds_equation(self):
        self.assertEqual(report_calculation_errors('10% × (13,422.20 − 2,624.81) = INR 1,079.74 Mn.'), [])
        self.assertEqual(report_calculation_errors('10% × (26,844.40 − 2,624.81) = INR 2,421.96 Mn.'), [])
        self.assertTrue(report_calculation_errors('10% × (13,422.20 − 2,624.81) = INR 2,079.74 Mn.'))
    def test_arithmetic_verification_does_not_depend_on_citation_validation(self):
        self.assertEqual(report_calculation_errors('MOIC = 100 / 50 = 2.00x [R999].'), [])
        self.assertTrue(report_calculation_errors('MOIC = 100 / 50 = 3.00x [R999].'))

    def test_caret_irr_checks_the_full_power_and_not_an_exponent_suffix(self):
        self.assertEqual(report_calculation_errors('IRR = 2.57^(1/5) − 1 = 20.8%.'), [])
        errors = report_calculation_errors('IRR = 3.85^(1/5) − 1 = 30.0%.')
        self.assertEqual(len(errors), 1)
        self.assertIn('3.85**(1/5)', errors[0])
        self.assertNotIn('-80.000000', errors[0])
        self.assertEqual(report_calculation_errors('CAGR = (200/100)^(1/2) - 1 = 41.4%.'), [])

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

    def test_currency_and_source_annotations_preserve_ratio_checks(self):
        self.assertEqual(report_calculation_errors('₹1.73 [R001] / ₹8.94 [R002] = 19.3%'), [])
        self.assertTrue(report_calculation_errors('₹1.73 [R001] / ₹8.94 [R002] = 25.3%'))

    def test_growth_and_multiplication_allow_display_rounding_but_reject_wrong_results(self):
        self.assertEqual(report_calculation_errors('(58.13 - 40.53) / 40.53 * 100 = 43.4%\n\n383.10 × 0.1713 = 65.63'), [])
        self.assertTrue(report_calculation_errors('(58.13 - 40.53) / 40.53 * 100 = 53.4%'))
        self.assertTrue(report_calculation_errors('383.10 × 0.1713 = 85.80'))

    def test_irr_propagates_rounded_proceeds_precision(self):
        self.assertEqual(report_calculation_errors('MOIC is 1.45 / 1 = 1.45x, and IRR is 9.8% over 4 years.'), [])
        self.assertTrue(report_calculation_errors('MOIC is 1.45 / 1 = 1.45x, and IRR is 11.8% over 4 years.'))
