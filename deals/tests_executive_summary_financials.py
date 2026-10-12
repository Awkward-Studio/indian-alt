from django.test import SimpleTestCase

from ai_orchestrator.test_executive_summary_format import fixture
from ai_orchestrator.services.executive_summary_format import executive_summary_format_errors
from deals.services.executive_summary_financials import align_summary_financials

KEY_FINANCIALS = '''## Key Financials

| Metric (INR Mn; unknown; target) | FY24 Unknown | FY26 Unknown | FY30 Unknown |
| --- | --- | --- | --- |
| Revenue | 791.73 [7] | 2081.87 [10] | 11780.28 [14] |
| EBITDA | -24.35 [32] | -203.01 [34] | 1815.23 [38] |
| PAT | -24.35 [64] | -217.94 [66] | 1223.96 [70] |
'''


class SummaryFinancialAlignmentTests(SimpleTestCase):
    def test_zouk_uses_same_annual_periods_native_units_and_negative_margin(self):
        result = align_summary_financials(fixture(), KEY_FINANCIALS)
        self.assertIn('| Metric | FY24 Unknown | FY26 Unknown | FY30 Unknown |', result)
        self.assertIn('| Revenue | 791.73 [1] | 2081.87 [1] | 11780.28 [1] |', result)
        self.assertIn('| EBITDA margin | -3.08% [1] | -9.75% [1] | 15.41% [1] |', result)
        self.assertIn('INR Mn; unknown; target', result)
        self.assertNotIn('FY24 Actual', result)
        self.assertEqual(executive_summary_format_errors(result), [])

    def test_zero_or_missing_inputs_do_not_produce_a_manufactured_margin(self):
        result = align_summary_financials(fixture(), KEY_FINANCIALS.replace('791.73 [7]', '0').replace('-203.01 [34]', 'Not provided'))
        self.assertIn('| EBITDA margin | Not provided | Not provided | 15.41% [1] |', result)

    def test_report_provenance_does_not_reuse_other_section_citation_numbers(self):
        result = align_summary_financials(fixture()+'\n\n### Citations\n\n5. Existing summary source.', KEY_FINANCIALS)
        self.assertIn('791.73 [6]', result)
        self.assertIn('6. Saved Key Financials report', result)
        self.assertNotIn('791.73 [7]', result)
        self.assertIn('5. Existing summary source.', result)

    def test_missing_financial_report_preserves_the_existing_summary(self):
        self.assertEqual(align_summary_financials(fixture(), ''), fixture())
