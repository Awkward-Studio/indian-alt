from django.test import SimpleTestCase
from ai_orchestrator.services.report_financial_format import source_unit_conversion_notes


class PrimaryStatementUnitConversionTests(SimpleTestCase):
    def test_live_audited_revenue_and_service_cost_convert_exactly(self):
        content = "Rs in '000\nMarch 2025\nRevenue from operations\n4,05,330.42\nPurchases of Services\n3,13,856.42"
        notes = source_unit_conversion_notes(content, 'normalized_text')
        self.assertIn('4,05,330.42 source units = INR 40.533042 Cr = INR 405.33042 million', notes)
        self.assertIn('3,13,856.42 source units = INR 31.385642 Cr = INR 313.85642 million', notes)
        self.assertNotIn('March 2025 source units', notes)

    def test_negative_values_keep_sign_and_eps_is_excluded(self):
        notes = source_unit_conversion_notes("INR lakhs\n(672.92)\nBasic Earning per share\n15.20", 'document_text')
        self.assertIn('INR -6.7292 Cr', notes)
        self.assertNotIn('15.20 source units', notes)

    def test_unknown_conflicting_or_summary_units_are_never_assumed(self):
        self.assertEqual(source_unit_conversion_notes('Revenue\n100.00', 'document_text'), '')
        self.assertEqual(source_unit_conversion_notes("Rs in '000\n100.00", 'table_summary'), '')
        self.assertEqual(source_unit_conversion_notes("Rs in '000\n100.00\nINR lakhs\n200.00", 'normalized_text'), '')
        self.assertEqual(source_unit_conversion_notes('USD millions\n100.00', 'normalized_text'), '')
