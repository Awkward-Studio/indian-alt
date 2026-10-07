from types import SimpleNamespace
from django.test import SimpleTestCase
from deals.services.ledger_financials import snapshot


def row(year, data, statement_type='profit_loss', basis='Standalone', source='local_ai'):
    return SimpleNamespace(id=year+statement_type+basis, fy=year, data=data,
        statement_type=statement_type, fin_type=basis, data_source=source, provenance={})


class LedgerFinancialsTests(SimpleTestCase):
    def test_verified_refresh_suppresses_stale_ai_periods_and_metrics(self):
        stale = row('FY25', {'revenue': 'INR 300 crore', 'ebitda_margin': '40%'})
        fresh = row('FY21', {'revenue': 'INR 7.02 crore', 'ebitda_margin': '20.34%'})
        fresh.provenance = {'metrics': {key: {'source': 'local_ai', 'extraction_contract': 'primary-financial-fields-v2'} for key in fresh.data}}
        result = snapshot([stale, fresh])
        self.assertEqual(result['fy'], 'FY21')
        self.assertEqual(result['revenue_cr'], '7.02')
        self.assertEqual(result['ebitda_margin_pct'], '20.34')

    def test_requested_metrics_and_derived_yoy_use_one_period_and_scope(self):
        result = snapshot([
            row('FY25', {'revenue': 'INR 347.727272727 crore'}),
            row('FY26', {'revenue': 'INR 459 crore', 'gross_margin': '40.5%', 'ebitda_margin': '11%'}),
            row('FY26', {'working_capital_days': '133'}, 'balance_sheet'),
            row('FY27E', {'revenue': 'INR 900 crore', 'gross_margin': '75%'}),
        ])
        self.assertEqual(result['fy'], 'FY26')
        self.assertEqual(result['revenue_cr'], '459')
        self.assertEqual(result['yoy_growth_pct'], '32')
        self.assertEqual(result['gross_margin_pct'], '40.5')
        self.assertEqual(result['ebitda_margin_pct'], '11')
        self.assertEqual(result['working_capital_days'], '133')

    def test_units_and_ratios_are_calculated_from_supported_amounts(self):
        result = snapshot([row('2025-26', {'revenue': 'INR 4590 million', 'gross_profit': 'INR 18589.5 lakh', 'ebitda': 'INR 504900 thousand'})])
        self.assertEqual(result['revenue_cr'], '459')
        self.assertEqual(result['gross_margin_pct'], '40.5')
        self.assertEqual(result['ebitda_margin_pct'], '11')

    def test_unknown_units_and_different_scope_do_not_create_numbers(self):
        result = snapshot([row('FY25', {'revenue': 'INR 100 crore'}, basis='Consolidated'),
            row('FY26', {'revenue': '459', 'ebitda': '50'}),
            row('FY26', {'working_capital_days': '133'}, 'balance_sheet', 'Consolidated')])
        self.assertIsNone(result['revenue_cr'])
        self.assertIsNone(result['yoy_growth_pct'])
        self.assertIsNone(result['ebitda_margin_pct'])
        self.assertIsNone(result['working_capital_days'])

    def test_missing_year_and_zero_prior_revenue_are_not_guessed(self):
        self.assertIsNone(snapshot([row('unknown', {'revenue': 'INR 10 crore'})]))
        result = snapshot([row('FY25', {'revenue': 'INR 0 crore'}), row('FY26', {'revenue': 'INR 10 crore'})])
        self.assertIsNone(result['yoy_growth_pct'])

    def test_forecasts_are_identified_and_foreign_currency_is_not_relabelled_inr(self):
        result = snapshot([row('FY27F', {'revenue': 'USD 100 million', 'gross_margin': '40.5%'})])
        self.assertEqual(result['fy'], 'FY27E')
        self.assertTrue(result['is_forecast'])
        self.assertIsNone(result['revenue_cr'])
