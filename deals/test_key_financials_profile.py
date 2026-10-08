from django.test import TestCase
from ai_orchestrator.services.report_financial_format import FINANCIAL_ROWS
from deals.models import Deal
from deals.services.key_financials_profile import section_payload, sync_section

def section():
    values = [459, 273.11, 185.89, 135.4, 50.49, 3, 47.49, 1, 2, 0, 48.49, 12, 36.49]
    return '## Key Financials\n\n| Metric (INR Cr) | FY26 Actual | FY27E Forecast |\n| --- | --- | --- |\n' + '\n'.join(
        f'| {name} | {amount:.2f} [1] | Not provided |' for name, amount in zip(FINANCIAL_ROWS, values))

class KeyFinancialsProfileTests(TestCase):
    def test_saved_draft_gaps_do_not_promote_unverified_figures_to_the_ledger(self):
        deal=Deal.objects.create(title='Flagged report')
        flagged=section()+'\n\n### Source gaps and calculation issues\n\n- **Gap:** Source amount not verified.'
        self.assertEqual(sync_section(deal,flagged,'report')['status'],'not_synced')
        self.assertFalse(deal.vi_relations.exists())
    def test_missing_citations_do_not_block_report_or_persist_unsupported_profile_values(self):
        deal = Deal.objects.create(title='Uncited report')
        uncited = section().replace(' [1]', '')
        payload, _, _ = section_payload(uncited, 'report')
        self.assertEqual(payload['financial_statements'], [])
        self.assertEqual(sync_section(deal, uncited, 'report')['status'], 'not_synced')

    def test_supplemental_tables_do_not_replace_main_statement_or_block_saving(self):
        extra = '| Metric | FY26 |\n| --- | --- |\n| Revenue | 999.00 |'
        payload, _, _ = section_payload(extra + '\n\n' + section() + '\n\n' + extra, 'report')
        self.assertEqual(payload['financial_statements'][0]['metrics']['revenue']['value'], '459.00 INR Cr')
        with self.assertRaises(ValueError):
            section_payload(extra, 'report')
        with self.assertRaises(ValueError):
            section_payload(section() + '\n\n' + section(), 'report')

    def test_bound_columns_do_not_overwrite_actual_profile_with_an_endpoint(self):
        bounded = section().replace('FY27E Forecast', 'FY27E Forecast (Lower bound)').replace('| Not provided |', '| 100.00 [1] |')
        payload, _, _ = section_payload(bounded, 'report')
        self.assertEqual(len(payload['financial_statements']), 1)
        self.assertEqual(payload['financial_statements'][0]['fy'], 'FY26')

    def test_accepted_table_drives_profile_without_another_model_request(self):
        deal = Deal.objects.create(title='Reviewed target')
        summary = sync_section(deal, section(), 'accepted-report')
        statement = deal.vi_relations.get().company_profile.financial_statements.get()
        self.assertEqual(statement.data['revenue'], '459.00 INR Cr')
        self.assertEqual(statement.data['ebitda'], '50.49 INR Cr')
        self.assertEqual(statement.data['ebitda_margin'], '11.00%')
        self.assertEqual(summary['source'], 'accepted_key_financials')
        self.assertNotIn('FY27E', list(deal.vi_relations.get().company_profile.financial_statements.values_list('fy', flat=True)))

    def test_native_units_are_preserved_and_never_assigned_crores(self):
        payload, _, _ = section_payload(section().replace('INR Cr', 'Native model units; currency/scale Not provided'), 'report')
        self.assertEqual(payload['financial_statements'][0]['metrics']['revenue']['value'], '459.00')
