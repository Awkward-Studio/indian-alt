from django.test import SimpleTestCase
from deals.services.financial_evidence import resolve_metric

class FinancialEvidenceTests(SimpleTestCase):
    def test_monthly_values_cannot_fill_annual_ledger_fields(self):
        source={'R001':{'financial_cells':{'E10':{'value':100,'period':'FY25','period_scope':'monthly',
            'row_label':'Total Income','unit_labels':['INR Mn'],'number_format':''}}}}
        self.assertEqual(resolve_metric('revenue','100 INR Mn','FY25',['R001'],source,{}),(None,[]))

    def test_primary_worksheet_units_override_generated_crore_guess(self):
        sources = {'R001': {'financial_cells': {'H53': {'value': 41.274181703511154,
                    'period': '2026-03-31 00:00:00', 'row_label': 'PBT', 'unit_labels': ['Currency: INR Mn'], 'number_format': '#,##0'}}}}
        value, refs = resolve_metric('profit_before_tax', '41.274181703511154 INR Cr', 'FY26', ['R001'], sources, {})
        self.assertEqual(value, '41.274181703511154 INR Mn')
        self.assertEqual(refs, ['R001'])
        self.assertEqual(resolve_metric('profit_before_tax', value, 'FY25', ['R001'], sources, {}), (None, []))

    def test_percentage_uses_saved_fraction_and_matching_source_period(self):
        sources = {'R001': {'financial_cells': {'H46': {'value': .0813, 'period': 'FY26',
                    'row_label': 'EBITDA%', 'unit_labels': ['INR Mn'], 'number_format': '0.00%'}}}}
        self.assertEqual(resolve_metric('ebitda_margin', '8.13%', 'FY26', ['R001'], sources, {}), ('8.13%', ['R001']))

    def test_generated_implied_unit_summary_is_not_primary_financial_evidence(self):
        self.assertEqual(resolve_metric('ebitda', '82.62 INR Cr', 'FY22', ['R001'],
            {'R001': {'locator': {'kind': 'metric'}}}, {'R001': '82.62 INR Cr (implied by scale)'}), (None, []))
