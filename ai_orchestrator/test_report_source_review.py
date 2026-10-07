from unittest.mock import Mock,patch
from django.test import SimpleTestCase,TestCase,override_settings
from ai_orchestrator.services.report_source_review import review_packet,validate_review,review_section


class ReportSourceReviewTests(SimpleTestCase):
    def test_saved_cell_json_and_all_blocks_are_preserved_without_nested_escaping(self):
        service = Mock()
        service.process_content.return_value = {'findings': [], 'coverage_gaps': []}
        cells = '{"period": "FY26E", "value": 90, "formula": "=SUM(A1:A3)"}'
        evidence = 'Retrieval block R001\n'+cells+'\n\nRetrieval block R002\nUncited coverage fact'
        review_section(ai_service=service, title='Transaction Details', draft='Equity [R001]',
                       evidence=evidence, source_id='test', requirements='Check funding')
        content = service.process_content.call_args.kwargs['content']
        self.assertIn(cells, content)
        self.assertIn('Uncited coverage fact', content)
        self.assertNotIn('\\"period\\"', content)
        self.assertEqual(service.process_content.call_args.kwargs['metadata']['_source_metadata']['review_source_ranks'], [1, 2])

    def test_source_review_keeps_its_section_delivery_generation(self):
        service = Mock()
        service.process_content.return_value = {'findings': [], 'coverage_gaps': []}
        review_section(ai_service=service, title='Key Financials', draft='Revenue [R001]',
            evidence='Retrieval block R001\nRevenue source', source_id='test', vdr_dispatch_generation=7)
        self.assertEqual(service.process_content.call_args.kwargs['metadata']['_source_metadata']['vdr_dispatch_generation'], 7)

    def test_all_primary_blocks_are_supplied_for_coverage_and_prior_drafts_are_excluded(self):
        evidence="Retrieval block R001\nRevenue source\n\nRetrieval block R002\nOther source\n\nCompleted sections from this report\nUnsupported draft"
        packet=review_packet('Key Financials','Revenue [R001@\'PL\'!R50].',evidence)
        self.assertEqual(len(packet['primary_evidence']),2)
        self.assertIn('Revenue source',packet['primary_evidence'][0])
        self.assertNotIn('Other source',packet['primary_evidence'][0])
        self.assertNotIn('Unsupported draft',packet['primary_evidence'][0])
        self.assertNotIn('Unsupported draft',packet['primary_evidence'][1])

    def test_missing_retrieval_block_is_not_treated_as_verified(self):
        with self.assertRaisesRegex(ValueError,'absent'):
            review_packet('Company Details','A claim [R002].','Retrieval block R001\nSource')

    def test_regeneration_feedback_is_not_treated_as_primary_evidence(self):
        packet = review_packet('Transaction Details', 'Claim [R001]',
            'Retrieval block R001\nPrimary source\n\n<prior_review_feedback>\nUnverified correction\n</prior_review_feedback>')
        self.assertNotIn('Unverified correction', packet['primary_evidence'][0])

    def test_review_cannot_invent_a_new_source_or_silently_pass_malformed_json(self):
        self.assertEqual(validate_review({'findings':[]},{1}),[])
        with self.assertRaises(ValueError):validate_review({'findings':[{'severity':'error','issue':'Mismatch','sources':['R999']}]},{1})
        with self.assertRaises(ValueError):validate_review({'approved':True},{1})

    def test_review_explicitly_confirming_no_error_cannot_block_the_section(self):
        findings = validate_review({'findings': [
            {'severity': 'error', 'issue': 'The claim is accurate', 'correction': 'No factual error. The analytical implication is valid.', 'sources': ['R001']},
            {'severity': 'error', 'issue': 'Wrong conversion', 'correction': 'Divide by 10,000,000', 'sources': ['R001']},
        ]}, {1})
        self.assertEqual([finding['severity'] for finding in findings], ['warning', 'error'])

    def test_live_correct_conversion_finding_is_not_reused_as_an_error(self):
        from ai_orchestrator.services.report_source_review import material_review_errors
        correct = {'severity': 'error', 'issue': 'Ensure unit consistency', 'sources': ['R001'],
            'correction': 'The value is ₹15.82 Mn (15,823,816.77 INR). The draft is numerically correct. No error here, moving to next.'}
        incorrect = {'severity': 'error', 'issue': 'Wrong conversion', 'sources': ['R001'],
            'correction': 'No error here in the revenue. However, EBITDA is overstated; use 1.36 Cr.'}
        self.assertEqual(material_review_errors([correct, incorrect]), [incorrect])
        findings = validate_review({'findings': [correct, incorrect]}, {1})
        self.assertEqual([f['severity'] for f in findings], ['warning', 'error'])

    def test_review_checks_conversion_basis_and_does_not_invent_missing_inputs(self):
        from ai_orchestrator.services.report_source_review import REVIEW_INSTRUCTIONS
        self.assertIn('dividing by 10,000', REVIEW_INSTRUCTIONS)
        self.assertIn('A funding ask is not enterprise value', REVIEW_INSTRUCTIONS)
        self.assertIn('explicitly disclosed missing input addresses coverage', REVIEW_INSTRUCTIONS)

    def test_false_rejection_is_verified_before_rewriting_the_report(self):
        service = Mock()
        service.process_content.side_effect = [
            {'findings': [{'severity': 'error', 'claim': 'Revenue INR 40.53 Cr', 'issue': 'Wrong conversion',
                'correction': 'Use INR 4.05 Cr', 'sources': ['R001']}], 'coverage_gaps': ['Tax history absent']},
            {'findings': [], 'coverage_gaps': []},
        ]
        review = review_section(ai_service=service, title='Key Financials',
            draft='Revenue INR 40.53 Cr [R001]. Tax history is not provided.',
            evidence="Retrieval block R001\nRs in '000\nRevenue 405330.42", source_id='test')
        self.assertEqual(review, {'findings': [], 'coverage_gaps': []})
        self.assertEqual(service.process_content.call_count, 2)
        self.assertIn('Verify the proposed review', service.process_content.call_args.kwargs['content'])

    def test_verified_error_requires_actual_claim_and_cited_evidence_quotes(self):
        from ai_orchestrator.services.report_source_review import validate_verified_findings
        finding = {'severity': 'error', 'claim': 'Revenue INR 4.05 Cr', 'issue': 'Wrong conversion',
            'error_type': 'contradiction', 'source_quote': 'Revenue 405330.42', 'sources': ['R001']}
        evidence = ['Retrieval block R001\nRevenue 405330.42', 'Retrieval block R002\nUnrelated quote']
        validate_verified_findings([finding], 'Revenue INR 4.05 Cr [R001]', evidence)
        with self.assertRaises(ValueError):
            validate_verified_findings([finding], 'Revenue INR 40.53 Cr [R001]', evidence)
        finding['source_quote'] = 'Unrelated quote'
        with self.assertRaises(ValueError):
            validate_verified_findings([finding], 'Revenue INR 4.05 Cr [R001]', evidence)

    def test_quote_formatting_does_not_create_false_review_failures(self):
        from ai_orchestrator.services.report_source_review import validate_verified_findings
        finding = {'severity':'error','claim':'The company is seeking $10 Mn', 'issue':'Unsupported ask',
                   'error_type':'unsupported'}
        validate_verified_findings([finding], 'the company is seeking **$10 Mn** [Deal Fields].', [])
        finding['claim'] = 'The company is seeking $100 Mn'
        with self.assertRaises(ValueError):
            validate_verified_findings([finding], 'the company is seeking **$10 Mn** [Deal Fields].', [])

    def test_malformed_review_quote_is_repaired_without_regenerating_the_draft(self):
        service = Mock()
        wrong = {'severity':'error','claim':'Revenue is exaggerated', 'issue':'Wrong revenue',
                 'error_type':'contradiction','source_quote':'Revenue 40.53 Cr','sources':['R001']}
        repaired = {**wrong, 'claim':'Revenue is INR 4.05 Cr'}
        service.process_content.side_effect = [
            {'findings':[wrong], 'coverage_gaps':[]},
            {'findings':[wrong], 'coverage_gaps':[]},
            {'findings':[repaired], 'coverage_gaps':[]},
        ]
        review = review_section(ai_service=service, title='Key Financials', draft='Revenue is INR 4.05 Cr [R001].',
            evidence='Retrieval block R001\nRevenue 40.53 Cr', source_id='test')
        self.assertEqual(review['findings'][0]['claim'], 'Revenue is INR 4.05 Cr')
        self.assertEqual(service.process_content.call_count, 3)
        self.assertTrue(service.process_content.call_args.kwargs['metadata']['_source_metadata']['review_response_repair'])


@override_settings(AI_INFERENCE_TARGET='h100',VDR_REPORT_SECTION_MIN_WORDS=900)
class SourceReviewedGenerationTests(TestCase):
    def setUp(self):
        from ai_orchestrator.services.pipeline_registry import PipelineRegistryService
        PipelineRegistryService.ensure_report_pipeline_defaults()

    @patch('ai_orchestrator.services.report_sections.cache')
    def test_complete_source_review_can_accept_a_near_minimum_section(self,cache):
        from ai_orchestrator.services.report_sections import ICReportSectionService
        cache.get.return_value=None
        service=Mock()
        service.process_content.side_effect=[
            {'response':'## Executive Summary\n\n'+('Supported analysis ' * 570)+' [R001].'},
            {'findings':[],'coverage_gaps':[]},
        ]
        result=ICReportSectionService._generate_section(ai_service=service,title='Executive Summary',
            evidence='Retrieval block R001\nPrimary source',analysis={'deal_model_data':{}},
            source_id='report-test',source_type='vdr_report_section',citations={'1':{'title':'IM.pdf','document_id':'doc'}},
            evidence_metadata={'selected_chunk_count':60},force_regenerate=True)
        self.assertIn('Supported analysis',result)
        self.assertEqual(service.process_content.call_count,2)
        generation=service.process_content.call_args_list[0].kwargs['metadata']
        self.assertTrue(generation['report_calculator'])
        self.assertTrue(generation['lossless_input'])
        self.assertFalse(generation['chat_template_kwargs']['enable_thinking'])
        review=service.process_content.call_args_list[1].kwargs['metadata']
        self.assertFalse(review['chat_template_kwargs']['enable_thinking'])

    @patch('ai_orchestrator.services.report_sections.cache')
    def test_source_error_cannot_pass_even_when_the_section_is_long(self,cache):
        from ai_orchestrator.services.report_sections import ICReportSectionService,ReportSectionStructureError
        cache.get.return_value=None
        service=Mock();service.process_content.side_effect=[
            {'response':'## Executive Summary\n\n'+('Lengthy analysis '*800)+'[R001].'},
            {'findings':[{'severity':'error','claim':'A claim','issue':'Not supported','sources':['R001'],'correction':'Mark a gap'}],'coverage_gaps':['Explain the investment approval gates']},
            {'findings':[{'severity':'error','claim':'Lengthy analysis','issue':'Not supported','sources':['R001'],
                'correction':'Mark a gap','error_type':'unsupported'}],'coverage_gaps':['Explain the investment approval gates']},
        ]
        with self.assertRaisesRegex(ReportSectionStructureError,'Source review rejected.*Explain the investment approval gates'):
            ICReportSectionService._generate_section(ai_service=service,title='Executive Summary',
                evidence='Retrieval block R001\nSource',analysis={'deal_model_data':{}},source_id='test',
                source_type='vdr_report_section',citations={'1':{'title':'IM.pdf','document_id':'doc'}},force_regenerate=True)
