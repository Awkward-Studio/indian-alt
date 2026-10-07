from unittest.mock import Mock,patch
from django.test import SimpleTestCase,TestCase,override_settings
from ai_orchestrator.services.report_source_review import review_packet,validate_review


class ReportSourceReviewTests(SimpleTestCase):
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

    def test_review_cannot_invent_a_new_source_or_silently_pass_malformed_json(self):
        self.assertEqual(validate_review({'findings':[]},{1}),[])
        with self.assertRaises(ValueError):validate_review({'findings':[{'severity':'error','issue':'Mismatch','sources':['R999']}]},{1})
        with self.assertRaises(ValueError):validate_review({'approved':True},{1})


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
            {'findings':[{'severity':'error','claim':'A claim','issue':'Not supported','sources':['R001'],'correction':'Mark a gap'}],'coverage_gaps':[]},
        ]
        with self.assertRaisesRegex(ReportSectionStructureError,'Source review rejected'):
            ICReportSectionService._generate_section(ai_service=service,title='Executive Summary',
                evidence='Retrieval block R001\nSource',analysis={'deal_model_data':{}},source_id='test',
                source_type='vdr_report_section',citations={'1':{'title':'IM.pdf','document_id':'doc'}},force_regenerate=True)
