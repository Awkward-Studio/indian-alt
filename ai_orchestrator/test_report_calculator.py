from unittest.mock import Mock,patch
from decimal import Decimal
from django.test import SimpleTestCase
from ai_orchestrator.services.report_calculator import calculate,calculation_request
from ai_orchestrator.services.llm_providers import VLLMProviderService


class ReportCalculatorTests(SimpleTestCase):
    def test_incomplete_calculator_json_is_repaired_instead_of_returned_as_report(self):
        malformed = '<report_calculations>\n[{"expression":"90/3",' + (' ' * 20000)
        with self.assertRaisesRegex(ValueError, 'complete JSON array'):
            calculation_request(malformed)
        first=Mock();first.json.return_value={'choices':[{'message':{'content':malformed},'finish_reason':'length'}]}
        last=Mock();last.json.return_value={'choices':[{'message':{'content':'## Company Details\nSupported final report [R001].'},'finish_reason':'stop'}]}
        with patch('ai_orchestrator.services.llm_providers.requests.post',side_effect=[first,last]) as post:
            result=VLLMProviderService().execute_standard({'model':'Qwen/test','prompt':'Write report','_report_calculator':True})
        self.assertIn('Supported final report',result['response'])
        self.assertEqual(result['_report_calculation_trace'][0]['rejected_request'],malformed)
        self.assertNotIn(malformed,post.call_args.kwargs['json']['messages'][-2]['content'])

    def test_units_returns_growth_and_discrepancies_use_decimal_arithmetic(self):
        self.assertEqual(Decimal(calculate('2071.93 * 1000 / 10000000')),Decimal('.207193'))
        self.assertAlmostEqual(float(calculate('100*((303/90)**(1/5)-1)')),27.4794038379585,places=9)
        self.assertEqual(Decimal(calculate('40.53-40.50')),Decimal('.03'))

    def test_arbitrary_code_and_unbounded_arithmetic_are_rejected(self):
        for expression in ['__import__("os").system("id")','[x for x in range(10)]','2**1000000','True+1','1/0']:
            with self.assertRaises((ValueError,ArithmeticError)): calculate(expression)

    def test_calculator_protocol_preserves_source_markers_and_reports_bad_inputs(self):
        results=calculation_request('<report_calculations>[{"expression":"180/60","label":"MOIC","source_markers":["R001"]},{"expression":"1/0"}]</report_calculations>')
        self.assertEqual(results[0]['result'],'3')
        self.assertEqual(results[0]['source_markers'],['R001'])
        self.assertIn('error',results[1])
        self.assertIsNone(calculation_request('Ordinary report text with no calculator request.'))

    def test_provider_returns_only_final_report_and_records_the_calculator_trace(self):
        first=Mock();first.json.return_value={'choices':[{'message':{'content':'<report_calculations>[{"expression":"180/60","label":"MOIC","source_markers":["R001"]}]</report_calculations>'},'finish_reason':'stop'}],'usage':{'prompt_tokens':10,'completion_tokens':10}}
        last=Mock();last.json.return_value={'choices':[{'message':{'content':'## Exit Considerations\n\nThe calculated MOIC is 3.0x [R001].'},'finish_reason':'stop'}],'usage':{'prompt_tokens':20,'completion_tokens':20}}
        with patch('ai_orchestrator.services.llm_providers.requests.post',side_effect=[first,last]) as post:
            result=VLLMProviderService().execute_standard({'model':'Qwen/test-model','prompt':'Write the report.','_report_calculator':True,'chat_template_kwargs':{'enable_thinking':False}})
        self.assertEqual(result['response'],'## Exit Considerations\n\nThe calculated MOIC is 3.0x [R001].')
        self.assertEqual(result['_report_calculation_trace'][0]['result'],'3')
        self.assertEqual(result['usage']['prompt_tokens'],30)
        self.assertEqual(post.call_count,2)
        self.assertIn('report_calculation_results',post.call_args.kwargs['json']['messages'][-1]['content'])
        self.assertTrue(post.call_args.kwargs['json']['messages'][-1]['content'].endswith('/no_think'))
