import json
from unittest.mock import MagicMock
from django.test import SimpleTestCase, override_settings
from ai_orchestrator.services.chat_document_chunks import ChatDocumentChunkService, split_utf8
from ai_orchestrator.services.llm_providers import VLLMProviderService


class ChatDocumentChunksTests(SimpleTestCase):
    def test_lossless_unicode_and_long_rows(self):
        text = ('वित्तीय खर्च ₹12345 🧾\n' * 4000) + 'LAST_ROW: 99'
        chunks = list(split_utf8(text, 12000))
        self.assertEqual(''.join(chunks), text)
        self.assertTrue(all(len(chunk.encode('utf-8')) <= 12000 for chunk in chunks))

    def test_all_files_and_tail_are_processed(self):
        provider = MagicMock()
        provider.execute_standard.return_value = {'response': 'Source notes: supported evidence.'}
        service = ChatDocumentChunkService(provider=provider, model='local')
        documents = [{'id': 'one', 'name': 'large.txt', 'text': 'start\n' * 30000 + 'TAIL_FACT'},
                     {'id': 'two', 'name': 'second.txt', 'text': 'SECOND_FILE_FACT'}]
        context, count = service.build_context(documents, 'Summarize all files')
        calls = provider.execute_standard.call_args_list
        prompts = '\n'.join(call.args[0]['prompt'] for call in calls)
        self.assertIn('TAIL_FACT', prompts)
        self.assertIn('SECOND_FILE_FACT', prompts)
        self.assertEqual(count, 2)
        self.assertLess(len(context.encode('utf-8')), 18500)
        self.assertTrue(all(call.args[0]['_enforce_context_budget'] for call in calls))

    def test_small_file_needs_no_map_call(self):
        provider = MagicMock()
        context, count = ChatDocumentChunkService(provider=provider, model='local').build_context(
            [{'name': 'small.txt', 'text': 'Revenue 100'}], 'Revenue?')
        provider.execute_standard.assert_not_called()
        self.assertIn('Revenue 100', context)
        self.assertEqual(count, 1)

    def test_empty_chunk_response_fails_instead_of_omitting_evidence(self):
        provider = MagicMock()
        provider.execute_standard.return_value = {'response': ''}
        with self.assertRaisesRegex(ValueError, 'incomplete evidence'):
            ChatDocumentChunkService(provider=provider, model='local').build_context(
                [{'name': 'large.txt', 'text': 'x' * 50000}], 'Summarize')

    def test_recursive_reduction_is_bounded(self):
        provider = MagicMock()
        provider.execute_standard.return_value = {'response': 'Evidence ' * 400}
        context, _ = ChatDocumentChunkService(provider=provider, model='local').build_context(
            [{'name': 'large.txt', 'text': 'x' * 200000}], 'Summarize')
        self.assertLess(len(context.encode('utf-8')), 18500)
        self.assertGreater(provider.execute_standard.call_count, 17)

    def test_complete_payload_budget_counts_estimated_input_and_output_tokens(self):
        provider = VLLMProviderService()
        payload = {'model': 'local', 'prompt': 'x', 'system': 'अ' * 180000,
                   'options': {'max_tokens': 4096}, '_enforce_context_budget': True}
        with self.assertRaisesRegex(ValueError, 'safe model context budget'):
            provider._build_chat_body(payload, stream=False)
        payload['system'] = 'Document assistant'
        body = provider._build_chat_body(payload, stream=True)
        self.assertEqual(body['max_tokens'], 4096)
        self.assertNotIn('_enforce_context_budget', body)

    @override_settings(CHAT_MODEL_CONTEXT_TOKENS=8192)
    def test_smaller_model_window_is_respected(self):
        with self.assertRaisesRegex(ValueError, 'safe model context budget'):
            VLLMProviderService()._build_chat_body({
                'model': 'local', 'prompt': 'x' * 12000,
                'options': {'max_tokens': 1000}, '_enforce_context_budget': True,
            }, stream=False)

    @override_settings(CHAT_MODEL_CONTEXT_TOKENS=65536)
    def test_report_split_allows_40k_input_and_reserves_16k_output(self):
        provider = VLLMProviderService()
        body = provider._build_chat_body({
            'model': 'local', 'prompt': 'x' * 120000,
            'options': {'max_tokens': 16384}, '_enforce_context_budget': True,
        }, stream=False)
        self.assertEqual(body['max_tokens'], 16384)
        with self.assertRaisesRegex(ValueError, 'safe model context budget'):
            provider._build_chat_body({
                'model': 'local', 'prompt': 'x' * 150000,
                'options': {'max_tokens': 16384}, '_enforce_context_budget': True,
            }, stream=False)

    @override_settings(CHAT_MODEL_CONTEXT_TOKENS=65536)
    def test_spreadsheet_escaping_fits_with_document_output_budget(self):
        provider = VLLMProviderService()
        prompt = "\t\n" * 5000

        with self.assertRaisesRegex(ValueError, 'safe model context budget'):
            provider._build_chat_body({
                'model': 'local', 'prompt': prompt,
                'options': {'max_tokens': 45056}, '_enforce_context_budget': True,
            }, stream=False)

        body = provider._build_chat_body({
            'model': 'local', 'prompt': prompt,
            'options': {'max_tokens': 32768}, '_enforce_context_budget': True,
        }, stream=False)
        self.assertEqual(body['max_tokens'], 32768)

    def test_cache_reuses_only_identical_question_model_and_private_scope(self):
        from unittest.mock import patch
        values = {}
        provider = MagicMock()
        provider.execute_standard.return_value = {'response': 'Complete source notes.'}
        service = ChatDocumentChunkService(provider=provider, model='local', cache_scope='user:conversation')
        with patch('ai_orchestrator.services.chat_document_chunks.cache') as cache:
            cache.get.side_effect = values.get
            cache.set.side_effect = lambda key, value, timeout: values.__setitem__(key, value)
            service._process('SOURCE', 'Question one')
            service._process('SOURCE', 'Question one')
            self.assertEqual(provider.execute_standard.call_count, 1)
            service._process('SOURCE', 'Question two')
            self.assertEqual(provider.execute_standard.call_count, 2)
            prompts = [call.args[0]['prompt'] for call in provider.execute_standard.call_args_list]
            self.assertEqual(prompts[0].split('QUESTION:')[0], prompts[1].split('QUESTION:')[0])
            service.cache_scope = 'other-user:other-conversation'
            service._process('SOURCE', 'Question one')
            self.assertEqual(provider.execute_standard.call_count, 3)

    def test_output_cutoff_is_not_cached_or_used(self):
        provider = MagicMock()
        provider.execute_standard.return_value = {'response': 'partial', 'raw': {'choices': [{'finish_reason': 'length'}]}}
        with self.assertRaisesRegex(ValueError, 'incomplete evidence'):
            ChatDocumentChunkService(provider=provider, model='local')._process('source', 'question')

    def test_zero_note_limit_omits_max_tokens_from_provider_request(self):
        provider = MagicMock()
        provider.execute_standard.return_value = {
            'response': 'Complete uncapped evidence note.',
            'raw': {'choices': [{'finish_reason': 'stop'}]},
        }

        service = ChatDocumentChunkService(
            provider=provider,
            model='local',
            note_max_tokens=0,
            request_timeout=1800,
        )
        service._process('dense spreadsheet evidence', 'Summarize all evidence')

        payload = provider.execute_standard.call_args.args[0]
        self.assertEqual(payload['options'], {'temperature': 0})
        self.assertNotIn('max_tokens', payload['options'])
        self.assertEqual(provider.execute_standard.call_args.kwargs['timeout'], 1800)

    def test_positive_note_limit_is_preserved(self):
        provider = MagicMock()
        provider.execute_standard.return_value = {'response': 'Complete capped note.'}

        service = ChatDocumentChunkService(
            provider=provider,
            model='local',
            note_max_tokens=2500,
        )
        service._process('source', 'question')

        payload = provider.execute_standard.call_args.args[0]
        self.assertEqual(payload['options']['max_tokens'], 2500)
        self.assertEqual(provider.execute_standard.call_args.kwargs['timeout'], 180)
