import io
import json
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from openpyxl import Workbook

from microsoft.services.email_ingestion import EmailIngestionService
from microsoft.services.email_matching import EmailDecisionService


class EmailLargeInputTests(SimpleTestCase):
    @patch('ai_orchestrator.services.ai_processor.AIProcessorService')
    @patch('ai_orchestrator.services.chat_document_chunks.ChatDocumentChunkService')
    def test_large_email_match_uses_all_section_reducer_and_bounded_ai_input(self, chunk_service, ai_service):
        source = ('Revenue and customer evidence\n' * 2000) + 'TAIL_COMPANY_IDENTITY'
        chunk_service.return_value.build_context.return_value = (
            '[Document email-1: Email body; chunk 50/50]\nTAIL_COMPANY_IDENTITY',
            1,
        )
        ai_service.return_value.process_content.return_value = {
            'parsed_json': {'deal_id': 'deal-1', 'evidence': 'TAIL_COMPANY_IDENTITY'},
        }

        result = EmailDecisionService.ai(
            'match',
            {'email': source, 'candidates': [{'deal_id': 'deal-1', 'title': 'Tail Company'}]},
            'email-1',
        )

        self.assertEqual(result['deal_id'], 'deal-1')
        documents, question = chunk_service.return_value.build_context.call_args.args
        self.assertEqual(documents[0]['text'], source)
        self.assertIn('company or deal', question)
        chunk_service.assert_called_once_with(cache_scope='email-ingestion:email-1:match')
        call = ai_service.return_value.process_content.call_args.kwargs
        self.assertLessEqual(len(call['content'].encode('utf-8')), EmailDecisionService.MAX_DECISION_INPUT_BYTES)
        self.assertIn('TAIL_COMPANY_IDENTITY', json.loads(call['content'])['email'])
        self.assertTrue(call['metadata']['enforce_context_budget'])

    @patch('ai_orchestrator.services.chat_document_chunks.ChatDocumentChunkService')
    def test_small_email_decision_does_not_invoke_reducer(self, chunk_service):
        payload = {'text': 'Summary\nMeeting notes from the call'}
        self.assertEqual(EmailDecisionService._bounded_payload('classify', payload, 'email-2'), payload)
        chunk_service.assert_not_called()

    def test_large_plain_text_attachment_keeps_tail(self):
        content = ('row,value\n' * 100_000 + 'TAIL_ATTACHMENT_VALUE,999').encode()
        text, extraction = EmailIngestionService.extract_attachment_text(
            content, 'large.csv', allow_remote=False,
        )
        self.assertIsNone(extraction)
        self.assertTrue(text.endswith('TAIL_ATTACHMENT_VALUE,999'))

    def test_large_workbook_uses_full_native_extraction(self):
        workbook = Workbook()
        workbook.active.title = 'First'
        workbook.active['A1'] = 'START_VALUE'
        workbook.create_sheet('Last')['Z200'] = 'TAIL_WORKBOOK_VALUE'
        output = io.BytesIO()
        workbook.save(output)
        workbook.close()

        text, extraction = EmailIngestionService.extract_attachment_text(
            output.getvalue(), 'large.xlsx', allow_remote=False,
        )

        self.assertIn('START_VALUE', text)
        self.assertIn('TAIL_WORKBOOK_VALUE', text)
        self.assertEqual(extraction['transcription_status'], 'complete')

