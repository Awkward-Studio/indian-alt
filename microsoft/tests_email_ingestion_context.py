from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.utils import timezone

from ai_orchestrator.models import DocumentChunk
from ai_orchestrator.services.universal_chat import UniversalChatService
from deals.models import Deal
from microsoft.models import Email, EmailAccount
from microsoft.services.email_evidence import EmailEvidenceService as Evidence
from microsoft.services.email_ingestion import EmailIngestionService as Ingestion


class EmailDealContextTests(TestCase):
    def setUp(self):
        account = EmailAccount.objects.create(email='context@example.test')
        self.deal = Deal.objects.create(title='Context company')
        self.other = Deal.objects.create(title='Other company')
        self.email = Email.objects.create(
            email_account=account,
            graph_id='context-email',
            subject='deal_name=Context company',
            body_text='The revised commitment is 177 crore.',
        )

    @staticmethod
    def _fake_vectorize_document(document):
        DocumentChunk.objects.update_or_create(
            deal=document.deal,
            source_type='document',
            source_id=str(document.id),
            defaults={
                'content': document.normalized_text,
                'search_text': document.normalized_text,
                'embedding': [0.1] * 1024,
                'embedding_model': 'test',
                'embedding_dimensions': 1024,
                'indexed_at': timezone.now(),
                'metadata': {'title': document.title, 'document_id': str(document.id)},
            },
        )
        document.is_indexed = True
        document.chunking_status = 'chunked'
        document.save(update_fields=['is_indexed', 'chunking_status'])
        return True

    @staticmethod
    def _fake_vectorize_meeting(note):
        deal = note.deals.get()
        DocumentChunk.objects.update_or_create(
            deal=deal,
            source_type='meeting_note',
            source_id=str(note.id),
            defaults={
                'content': note.body,
                'search_text': note.body,
                'embedding': [0.2] * 1024,
                'embedding_model': 'test',
                'embedding_dimensions': 1024,
                'indexed_at': timezone.now(),
                'metadata': {'title': note.title, 'source_email_id': str(note.source_email_id)},
            },
        )
        note.is_indexed = True
        note.chunk_count = 1
        note.save(update_fields=['is_indexed', 'chunk_count'])
        return True

    @patch('ai_orchestrator.services.embedding_processor.EmbeddingService.refresh_deal_profile')
    @patch('ai_orchestrator.services.embedding_processor.EmbeddingService.is_embedding_available', return_value=True)
    @patch('ai_orchestrator.services.embedding_processor.EmbeddingService.vectorize_document', _fake_vectorize_document)
    def test_new_reply_is_available_only_in_matched_deal_context(self, available, refresh):
        DocumentChunk.objects.create(deal=self.deal, source_type='email', source_id=str(self.email.id),
                                     content=self.email.body_text, search_text=self.email.body_text)
        run = Evidence.snapshot(self.email)
        self.assertEqual(Ingestion.process(run.id, use_ai=False)['status'], 'completed')
        document = self.deal.documents.get()
        self.assertFalse(DocumentChunk.objects.filter(source_type='email', source_id=str(self.email.id)).exists())
        chunk = DocumentChunk.objects.get(source_type='document', source_id=str(document.id))
        self.assertIn('177 crore', chunk.content)
        self.assertFalse(DocumentChunk.objects.filter(deal=self.other, content__contains='177 crore').exists())

        service = UniversalChatService.__new__(UniversalChatService)
        service._document_cache = {}
        service.flow_config = {'stages': {'context_assembly': {'chunk_excerpt_chars': 1400}}}
        service.disable_hard_caps = False
        serialized = service._serialize_chunk({'chunk': chunk, 'score': 1.0})
        self.assertEqual(serialized['deal_id'], str(self.deal.id))
        self.assertEqual(serialized['source_id'], str(document.id))
        self.assertIn('177 crore', serialized['text'])
        self.assertEqual(serialized['source_title'], document.title)

    @patch('ai_orchestrator.services.embedding_processor.EmbeddingService.refresh_deal_profile')
    @patch('ai_orchestrator.services.embedding_processor.EmbeddingService.is_embedding_available', return_value=True)
    @patch('ai_orchestrator.services.embedding_processor.EmbeddingService.vectorize_meeting_note', _fake_vectorize_meeting)
    def test_meeting_keeps_meeting_note_source_and_citation(self, available, refresh):
        self.email.body_text = 'Summary\nDiscussed financing\nTranscript\nThe board approved 88 crore.'
        self.email.save()
        run = Evidence.snapshot(self.email)
        self.assertEqual(Ingestion.process(run.id, use_ai=False)['status'], 'completed')
        note = self.deal.meeting_notes.get()

        service = UniversalChatService.__new__(UniversalChatService)
        service._document_cache = {}
        service.flow_config = {'stages': {'context_assembly': {'chunk_excerpt_chars': 1400}}}
        service.disable_hard_caps = False
        chunks, diagnostics = service.chunks_for_selected_transcripts(
            deal_id=str(self.deal.id), transcript_ids=[str(note.id)])
        self.assertEqual(diagnostics['returned_transcript_chunk_count'], 1)
        self.assertEqual(chunks[0]['source_type'], 'meeting_note')
        self.assertEqual(chunks[0]['source_id'], str(note.id))
        self.assertIn('88 crore', chunks[0]['text'])

    @patch('ai_orchestrator.services.embedding_processor.EmbeddingService.is_embedding_available', return_value=True)
    @patch('ai_orchestrator.services.embedding_processor.EmbeddingService.vectorize_document', _fake_vectorize_document)
    def test_retry_does_not_duplicate_retrieval_chunks(self, available):
        run = Evidence.snapshot(self.email)
        Ingestion.process(run.id, use_ai=False)
        run.refresh_from_db()
        run.status = 'waiting_service'
        run.stages['index'] = 'waiting_service'
        run.next_attempt_at = None
        run.save()
        Ingestion.process(run.id, use_ai=False)
        self.assertEqual(DocumentChunk.objects.filter(deal=self.deal, source_type='document').count(), 1)
