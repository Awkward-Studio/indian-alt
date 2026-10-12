from django.test import SimpleTestCase, override_settings
from unittest.mock import patch

from ai_orchestrator.services.token_budget import (
    pipeline_input_budget, pipeline_evidence_budget, expanded_context_chars, ContextBudgetExceeded,
)
from ai_orchestrator.services.chat_document_chunks import ChatDocumentChunkService
from ai_orchestrator.services.universal_chat import UniversalChatService
from ai_orchestrator.services.prompts import PromptBuilderService
from ai_orchestrator.services.llm_providers import VLLMProviderService
from deals.services.deal_field_synthesis import DealFieldSynthesisService
from microsoft.services.email_matching import EmailDecisionService


@override_settings(AI_INFERENCE_TARGET='h100', CHAT_MODEL_CONTEXT_TOKENS=262144,
                   AI_PIPELINE_INPUT_TOKENS=229376)
class PipelineContextBudgetTests(SimpleTestCase):
    def test_output_and_instruction_reservations(self):
        self.assertEqual(pipeline_input_budget(), 229376)
        self.assertEqual(pipeline_input_budget(32768), 225280)
        self.assertEqual(pipeline_evidence_budget(), 212992)
        self.assertLess(pipeline_evidence_budget(32768) + 32768 + 4096, 262144)

    def test_chat_upload_preserves_source_above_previous_reduction_limit(self):
        service = ChatDocumentChunkService(provider=object(), model='local')
        with patch.object(service, '_process', side_effect=AssertionError('Unexpected reduction')):
            source = 'financial evidence ' * 12000
            context, count = service.build_context([{'id':'source','name':'file','text':source}], 'read it')
        self.assertIn(source, context)
        self.assertEqual(count, 1)

    def test_legacy_published_chat_settings_expand_at_runtime_without_mutation(self):
        service = UniversalChatService.__new__(UniversalChatService)
        service.flow_config = {'stages':[{'id':'context_assembly','settings':{
            'max_context_chars':90000,'max_total_chunks':48,'max_chunks_per_selected_deal':12}}]}
        resolved = service._stage_settings('context_assembly')
        self.assertEqual(resolved['max_context_chars'], 425984)
        self.assertEqual(resolved['max_total_chunks'], 192)
        self.assertEqual(service.flow_config['stages'][0]['settings']['max_total_chunks'], 48)
        service.flow_config['stages'].append({'id':'chunk_retrieval','settings':{
            'vector_limit':120,'fallback_candidate_limit':160,'synthesis_document_candidate_limit':72}})
        retrieval = service._stage_settings('chunk_retrieval')
        self.assertEqual(retrieval['vector_limit'], 480)
        self.assertEqual(retrieval['fallback_candidate_limit'], 640)
        self.assertEqual(retrieval['synthesis_document_candidate_limit'], 288)

    def test_prompt_and_synthesis_retain_larger_source(self):
        source = 'revenue evidence ' * 14000
        prompt, _ = PromptBuilderService.build_user_prompt('{{content}}', source)
        self.assertEqual(prompt, source.strip())
        chars, tokens = DealFieldSynthesisService._context_limits()
        self.assertGreater(chars, 144000)
        self.assertGreater(tokens, 60000)

    def test_email_does_not_reduce_source_that_fits_in_large_window(self):
        source = 'email source ' * 5000
        payload = {'text':source}
        with patch('ai_orchestrator.services.chat_document_chunks.ChatDocumentChunkService.build_context',
                   side_effect=AssertionError('Unexpected reduction')):
            result = EmailDecisionService._bounded_payload('classify', payload, 'source')
        self.assertEqual(result, payload)

    def test_complete_request_checked_without_caller_opt_in(self):
        provider = VLLMProviderService()
        with self.assertRaises(ContextBudgetExceeded):
            provider._build_chat_body({'model':'local','prompt':'x' * 900000,
                                       'options':{'max_tokens':16384}}, stream=False)

    @override_settings(AI_INFERENCE_TARGET='t4', CHAT_MODEL_CONTEXT_TOKENS=65536)
    def test_t4_keeps_existing_evidence_limits(self):
        self.assertEqual(expanded_context_chars(18000), 18000)
        self.assertEqual(DealFieldSynthesisService._context_limits(), (72000,30000))
        self.assertLessEqual(pipeline_input_budget(), 65536 - 16384 - 4096)
