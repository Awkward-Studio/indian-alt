from types import SimpleNamespace
from decimal import Decimal
from django.test import SimpleTestCase, TestCase
from deals.services.financial_snapshot import build_snapshot, period_from_headers, render_main_table, units_from_labels, persist_snapshot, load_snapshot
from deals.models import Deal, DealDocument, FinancialFact, FinancialSnapshot


def document(rows, identity='document', merged=None):
    return SimpleNamespace(id=identity, title='Datapack.xlsx', extraction_manifest={'content_sha256': identity, 'workbook': {'sheet_order': ['Channel Mix']}, 'sheets': [{'name': 'Channel Mix', 'merged_ranges': merged or [], 'cells': [{'coordinate': a, 'value': v} for a,v in rows]}]})


def quarters():
    return [('B1', 'INR Mn'), ('C2', 'FY24 Actual'), ('D2','FY24 Actual'), ('E2','FY24 Actual'), ('F2','FY24 Actual'), ('C3','Q1FY24'), ('D3','Q2FY24'), ('E3','Q3FY24'), ('F3','Q4FY24'), ('B9','Net Sales'), ('C9','159.96326354053537'), ('D9','185.56229774544704'), ('E9','228.04555191607054'), ('F9','213.94716358035714')]


class FinancialSnapshotTests(SimpleTestCase):
    def test_zouk_quarters_retained_and_deterministically_aggregated(self):
        snapshot=build_snapshot([document(quarters())])
        annual=next(c for c in snapshot['cells'] if c['period']['scope']=='annual')
        self.assertEqual(annual['value'],'787518276.78241009000000')
        self.assertEqual(annual['state'],'derived')
        self.assertEqual(len(annual['fact_ids']),4)
        table,gaps=render_main_table(snapshot,{})
        self.assertIn('787.52',table)
        self.assertIn('FY24 Actual',table)

    def test_partial_quarters_are_not_annualized(self):
        snapshot=build_snapshot([document(quarters()[:-2])])
        self.assertFalse(any(c['period']['scope']=='annual' for c in snapshot['cells']))

    def test_unknown_zero_formula_and_unmapped_values_are_retained(self):
        snapshot=build_snapshot([document([('A1','FY25 Actual'),('B2',0),('B3','=X99'),('A4','Unfamiliar financial label'),('B4',-12)])])
        self.assertEqual(len(snapshot['facts']),3)
        self.assertEqual(snapshot['facts'][0]['value'],'0')
        self.assertEqual(snapshot['facts'][1]['state'],'missing_cache')

    def test_quarter_parent_header_does_not_become_annual(self):
        p=period_from_headers(['FY25 Actual','Q2FY25'])
        self.assertEqual((p['scope'],p['index'],p['status']),('quarterly',2,'actual'))
        self.assertEqual(period_from_headers(['H1 FY26 Actual'])['scope'],'half_year')

    def test_currency_never_defaults_to_inr(self):
        self.assertIsNone(units_from_labels(['Mn'])['currency'])
        self.assertIsNone(units_from_labels(['USD INR Mn'])['currency'])

    def test_conflicting_values_are_preserved_and_not_selected(self):
        rows=[('B1','INR Mn'),('C2','FY25 Actual'),('B9','Revenue'),('C9','100')]
        snapshot=build_snapshot([document(rows,identity='one'),document(rows[:-1]+[('C9','101')],identity='two')])
        cell=snapshot['cells'][0]
        self.assertEqual(cell['state'],'conflicted')
        self.assertEqual(len(cell['alternatives']),2)
        self.assertIsNone(cell['value'])

    def test_merged_header_reaches_all_quarter_columns(self):
        rows=[(a,v) for a,v in quarters() if a not in {'D2','E2','F2'}]
        snapshot=build_snapshot([document(rows,merged=['C2:F2'])])
        self.assertTrue(any(c['period']['scope']=='annual' for c in snapshot['cells']))

    def test_source_truncation_is_a_coverage_failure(self):
        doc=document(quarters());doc.extraction_manifest['truncated']=True
        snapshot=build_snapshot([doc])
        self.assertEqual(snapshot['coverage']['status'],'incomplete')

    def test_stock_balances_are_not_summed(self):
        snapshot=build_snapshot([document([(a,'Cash and cash equivalents' if a=='B9' else v) for a,v in quarters()])])
        self.assertFalse(any(c['period']['scope']=='annual' for c in snapshot['cells']))


class FinancialSnapshotPersistenceTests(TestCase):
    def test_replay_and_source_versions_are_immutable(self):
        deal=Deal.objects.create(title='Zouk regression')
        doc=DealDocument.objects.create(deal=deal,title='Datapack.xlsx')
        source=document(quarters(),identity=str(doc.id))
        first=build_snapshot([source]);identity=persist_snapshot(deal,first)
        self.assertEqual(persist_snapshot(deal,first),identity)
        self.assertEqual(FinancialFact.objects.count(),4)
        source.extraction_manifest['content_sha256']='new-version';source.extraction_manifest['sheets'][0]['cells'][-1]['value']='214'
        second=build_snapshot([source]);other=persist_snapshot(deal,second)
        self.assertNotEqual(identity,other)
        self.assertEqual(FinancialSnapshot.objects.count(),2)
        self.assertEqual(load_snapshot(identity)['cells'],first['cells'])


class SnapshotIntegrityTests(SimpleTestCase):
    def test_supported_tax_and_pat_survive_percent_label_with_explicit_amount_unit(self):
        rows=[('B1','INR Mn'),('C2','FY25 Actual'),('B4','PBT'),('C4',100),('B5','Tax'),('C5',25),('B6','PAT%'),('C6',75),('B7','Revenue'),('C7',200)]
        snapshot=build_snapshot([document(rows)])
        table,_=render_main_table(snapshot,{})
        self.assertIn('| Income Tax Expense | 25.00',table)
        self.assertIn('| PAT | 75.00',table)

    def test_missing_finance_costs_do_not_create_a_pbt_assumption(self):
        rows=[('B1','INR Mn'),('C2','FY25 Actual'),('B4','EBITDA'),('C4',100),('B5','Depreciation'),('C5',10),('B7','Revenue'),('C7',200)]
        snapshot=build_snapshot([document(rows)])
        table,_=render_main_table(snapshot,{})
        self.assertIn('| EBIT | 90.00',table)
        self.assertIn('| PBT | Not provided',table)

    def test_source_input_verifier_rejects_tampered_snapshot(self):
        from deals.services.financial_snapshot import verified_cell_value
        snapshot=build_snapshot([document(quarters())])
        annual=next(c for c in snapshot['cells'] if c['period']['scope']=='annual')
        original=verified_cell_value(annual,snapshot['facts'])
        annual['value']='123'
        self.assertNotEqual(original,Decimal(annual['value']))
        table,_=render_main_table(snapshot,{})
        self.assertNotIn('123.00',table)

    def test_native_pdf_financial_table_retains_source_locators(self):
        doc=SimpleNamespace(id='pdf',title='Audited.pdf',extracted_text='',extraction_manifest={'kind':'document_pages','pages':[{'page':3,'provenance':'native_text','status':'complete','text':'Amounts in INR Mn','tables':[[['Metric (INR Mn)','FY25 Actual'],['Revenue','100'],['PAT','10']]]}]})
        snapshot=build_snapshot([doc])
        self.assertEqual(snapshot['facts'][0]['locator']['page'],3)
        table,_=render_main_table(snapshot,{})
        self.assertIn('100.00',table)

    def test_plain_year_headers_and_source_native_units_are_not_dropped(self):
        rows=[('B2',2024),('C2',2025),('A4','Revenues'),('B4',100),('C4',200)]
        snapshot=build_snapshot([document(rows)])
        table,_=render_main_table(snapshot,{})
        self.assertIn('native model units',table)
        self.assertIn('100.00',table)
        self.assertIn('200.00',table)
        self.assertTrue(any(issue['kind']=='units_unknown' for issue in snapshot['issues']))

    def test_annual_year_end_dates_do_not_turn_into_monthly_values(self):
        rows=[('B1','INR Mn'),('C2','2024-03-31'),('D2','2025-03-31'),('B4','Revenue'),('C4',100),('D4',200)]
        snapshot=build_snapshot([document(rows)])
        self.assertEqual({cell['period']['scope'] for cell in snapshot['cells']},{'annual'})

    def test_source_operating_revenue_is_not_conflated_with_total_revenue(self):
        rows=[('B1','INR Mn'),('C2','FY25 Actual'),('B4','Revenue from operations'),('C4',100),('B5','Total Revenue'),('C5',105)]
        snapshot=build_snapshot([document(rows)])
        table,_=render_main_table(snapshot,{})
        self.assertIn('100.00',table)
        self.assertNotIn('105.00',table)
        self.assertEqual(len(snapshot['facts']),2)

    def test_tax_above_net_sales_is_not_assumed_to_be_income_tax(self):
        rows=[('B1','INR Mn'),('C2','FY25 Actual'),('B4','Tax'),('C4',18),('B5','Net Sales'),('C5',100)]
        snapshot=build_snapshot([document(rows)])
        table,_=render_main_table(snapshot,{})
        self.assertIn('| Income Tax Expense | Not provided',table)
        self.assertTrue(any(fact['metric']=='unclassified_tax' for fact in snapshot['facts']))

    def test_annual_summary_survives_monthly_columns_on_the_same_header_row(self):
        rows=[('B1','INR Mn'),('C2','2024-01-31'),('D2','2024-02-29'),('E2','2024-03-31'),('F2','2025-03-31'),('G2','2026-03-31'),('B4','Revenue'),('C4',1),('D4',2),('E4',100),('F4',200),('G4',300)]
        snapshot=build_snapshot([document(rows)])
        annual=[cell for cell in snapshot['cells'] if cell['period']['scope']=='annual']
        self.assertEqual(len(annual),3)
        self.assertEqual(len([cell for cell in snapshot['cells'] if cell['period']['scope']=='monthly']),2)
