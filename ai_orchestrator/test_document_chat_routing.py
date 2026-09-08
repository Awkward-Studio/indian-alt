from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIRequestFactory, force_authenticate
from ai_orchestrator.models import AIConversation
from ai_orchestrator.services.universal_chat import UniversalChatService
from ai_orchestrator.views import UniversalChatDocumentView


class DocumentChatRoutingTests(SimpleTestCase):
    @patch('ai_orchestrator.models.AIConversation.objects.filter')
    def test_upload_scope_skips_deal_discovery_even_after_unrelated_answer(self, conversations):
        conversations.return_value.first.return_value = AIConversation(metadata={
            'chat_documents': [{'name': 'budget.xlsx', 'text': 'Budget: 100'}],
        })
        service = object.__new__(UniversalChatService)
        service._stage_settings = MagicMock(return_value={})
        service._decide_query_builder_usage = MagicMock(side_effect=AssertionError('Must not search deals'))
        result = service.process_intent_and_build_metadata(
            'Summarize this file', '895a66ae-d765-43f5-91b3-b0a7430a8efa',
            'ASSISTANT: Project Zenobia is a RegTech company', 'audit',
        )
        self.assertEqual(result['gate_mode'], 'uploaded_documents')
        self.assertEqual(result['selected_sources'], [])
        self.assertEqual(result['deals_considered'], 0)
        self.assertFalse(result['used_query_builder'])
        self.assertIn('Prior assistant claims are not evidence', result['context_data'])

    @patch('ai_orchestrator.views.ChatDocumentEvidenceService.build', return_value={
        'evidence': {}, 'text': 'Budget 100', 'truncated': False, 'artifact_status': 'ready',
    })
    @patch('ai_orchestrator.views.DocumentProcessorService')
    @patch('ai_orchestrator.views.transaction.atomic', side_effect=lambda: nullcontext())
    @patch('ai_orchestrator.views.AIConversation.objects.create')
    @patch('ai_orchestrator.views.Deal.objects.get')
    def test_first_deal_upload_creates_deal_scoped_conversation(self, deals, create, atomic, processor, build):
        deals.return_value = SimpleNamespace(id='deal-id', title='Example deal')
        conversation = MagicMock(metadata={})
        create.return_value = conversation
        processor.return_value.get_chat_extraction_result.return_value = {'text': 'Budget 100'}
        request = APIRequestFactory().post('/upload', {
            'file': SimpleUploadedFile('budget.txt', b'Budget 100'), 'deal_id': 'deal-id',
        }, format='multipart')
        force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
        response = UniversalChatDocumentView.as_view()(request)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(create.call_args.kwargs['metadata'], {
            'kind': 'deal_chat', 'deal_id': 'deal-id', 'deal_title': 'Example deal',
        })

    def test_deal_context_requires_current_explicit_request(self):
        from ai_orchestrator.services.chat_documents import requests_deal_context
        for message in ['Summarize this file', 'List the risks in this deal memo', 'Use only the document', 'Do not include deal context', 'Summarize without deal context']:
            with self.subTest(message=message):
                self.assertFalse(requests_deal_context(message))
        for message in ['Compare this document with the deal', 'Include deal context', 'Compare this budget with companies in our portfolio']:
            with self.subTest(message=message):
                self.assertTrue(requests_deal_context(message))

    @patch('ai_orchestrator.models.AIConversation.objects.filter')
    def test_explicit_deal_request_reaches_retrieval(self, conversations):
        conversations.return_value.first.return_value = AIConversation(metadata={
            'chat_documents': [{'name': 'budget.xlsx', 'text': 'Budget: 100'}],
        })
        service = object.__new__(UniversalChatService)
        service._stage_settings = MagicMock(return_value={})
        service._decide_query_builder_usage = MagicMock(side_effect=RuntimeError('retrieval reached'))
        with self.assertRaisesRegex(RuntimeError, 'retrieval reached'):
            service.process_intent_and_build_metadata('Compare this file with the deal',
                '895a66ae-d765-43f5-91b3-b0a7430a8efa', '', 'audit')
