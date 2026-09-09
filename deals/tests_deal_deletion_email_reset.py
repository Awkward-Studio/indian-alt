"""Tests verifying connected emails can be re-processed when a deal is deleted."""
from unittest.mock import patch
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import Profile
from ai_orchestrator.models import AIAuditLog, DocumentChunk
from deals.models import Deal, DealDocument
from microsoft.models import Email, EmailAccount, EmailIngestionRun, EmailEvidenceLink
from microsoft.serializers import EmailListSerializer
from microsoft.services.email_evidence import EmailEvidenceService as Evidence
from microsoft.services.email_ingestion import EmailIngestionService as Ingestion


@override_settings(EMAIL_INGESTION_ENABLED=True)
class DealDeletionEmailResetTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(username='admin-deal-delete', password='test-only', email='admin@example.test')
        Profile.objects.create(user=self.user, email=self.user.email, is_admin=True)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

        self.account = EmailAccount.objects.create(email='inbound@example.test')
        self.deal = Deal.objects.create(title='Acme Corp')
        self.email = Email.objects.create(
            email_account=self.account,
            graph_id='graph-test-msg-1',
            subject='Investment Opportunity: Acme Corp',
            body_text='Pitch deck attached for Acme Corp series A.',
            deal=self.deal,
            is_processed=True,
            is_indexed=True,
            processing_status='completed',
            analysis_result={'summary': 'Great company'},
        )
        self.deal.source_email_id = self.email.graph_id
        self.deal.save(update_fields=['source_email_id'])

        self.run = EmailIngestionRun.objects.create(
            email=self.email,
            input_version='v1-test',
            status='completed',
            match={'status': 'matched', 'deal_id': str(self.deal.id), 'title': self.deal.title},
        )
        self.chunk = DocumentChunk.objects.create(
            deal=self.deal,
            source_type='email',
            source_id=str(self.email.id),
            content='Pitch deck attached',
        )

    def test_direct_deal_deletion_resets_email_and_cleans_runs(self):
        self.assertEqual(self.email.deal, self.deal)
        self.assertTrue(self.email.is_processed)

        # Delete the deal
        self.deal.delete()

        self.email.refresh_from_db()
        self.assertIsNone(self.email.deal)
        self.assertFalse(self.email.is_processed)
        self.assertFalse(self.email.is_indexed)
        self.assertEqual(self.email.processing_status, 'idle')
        self.assertEqual(self.email.analysis_result, {})
        self.assertIsNone(self.email.processed_at)

        # Ingestion runs and chunks removed
        self.assertFalse(EmailIngestionRun.objects.filter(email=self.email).exists())
        self.assertFalse(DocumentChunk.objects.filter(source_id=str(self.email.id)).exists())

        # Email serializer shows unlinked and pending
        data = EmailListSerializer(self.email).data
        self.assertIsNone(data.get('deal_id'))
        self.assertIsNone(data.get('deal_title'))
        ingestion_data = data['latest_ingestion']
        self.assertEqual(ingestion_data['status'], 'pending')
        self.assertIsNone(ingestion_data['matched_deal_id'])
        self.assertIsNone(ingestion_data['matched_deal_title'])
        self.assertEqual(ingestion_data['deal_match_status'], 'unmatched')

    @patch.object(Ingestion, 'dispatch')
    def test_email_can_be_reprocessed_after_deal_deletion(self, mock_dispatch):
        self.deal.delete()

        # Re-enqueuing the email now succeeds and creates a fresh run
        with self.captureOnCommitCallbacks(execute=True):
            fresh_run = Ingestion.enqueue(self.email)
        self.assertIsNotNone(fresh_run)
        self.assertNotEqual(str(fresh_run.id), str(self.run.id))
        self.assertEqual(fresh_run.status, 'pending')
        mock_dispatch.assert_called_once_with(fresh_run.id)

    def test_api_deal_deletion_resets_connected_email(self):
        response = self.client.delete(f'/api/deals/{self.deal.id}/')
        self.assertEqual(response.status_code, 204)

        self.assertFalse(Deal.objects.filter(id=self.deal.id).exists())

        self.email.refresh_from_db()
        self.assertIsNone(self.email.deal)
        self.assertFalse(self.email.is_processed)
        self.assertEqual(self.email.processing_status, 'idle')
        self.assertFalse(EmailIngestionRun.objects.filter(email=self.email).exists())

    def test_unlink_onedrive_cleans_documents_and_resets_deal(self):
        self.deal.source_onedrive_id = 'folder-123'
        self.deal.source_drive_id = 'drive-123'
        self.deal.processing_status = 'processing'
        self.deal.save()

        doc = DealDocument.objects.create(
            deal=self.deal,
            title='Pitch.pdf',
            onedrive_id='file-1',
        )
        DocumentChunk.objects.create(
            deal=self.deal,
            source_type='document',
            source_id=str(doc.id),
            content='Pitch deck chunk',
        )
        parent_audit = AIAuditLog.objects.create(
            source_type='vdr_indexing', source_id=str(self.deal.id),
            model_used='test', system_prompt='test', user_prompt='test',
            status='PROCESSING', is_success=False,
            source_metadata={'child_task_ids': ['child-1']},
        )
        segment_audit = AIAuditLog.objects.create(
            source_type='document_evidence_segment', source_id=str(doc.id),
            model_used='test', system_prompt='test', user_prompt='test',
            status='PROCESSING', is_success=False,
        )

        response = self.client.post(f'/api/deals/{self.deal.id}/unlink_onedrive/')
        self.assertEqual(response.status_code, 200)

        self.deal.refresh_from_db()
        self.assertIsNone(self.deal.source_onedrive_id)
        self.assertIsNone(self.deal.source_drive_id)
        self.assertEqual(self.deal.processing_status, 'idle')
        self.assertFalse(DealDocument.objects.filter(id=doc.id).exists())
        self.assertFalse(DocumentChunk.objects.filter(source_id=str(doc.id)).exists())
        parent_audit.refresh_from_db()
        segment_audit.refresh_from_db()
        self.assertTrue(parent_audit.source_metadata['cancel_requested'])
        self.assertEqual(parent_audit.source_metadata['cancel_reason'], 'folder_unlinked')
        self.assertEqual(parent_audit.status, 'FAILED')
        self.assertIsNotNone(parent_audit.completed_at)
        self.assertEqual(segment_audit.status, 'FAILED')
        self.assertIn('linked VDR folder was removed', segment_audit.error_message)

    @patch('deals.tasks.prepare_linked_folder_vdr_async.apply_async')
    def test_relink_onedrive_replaces_old_folder_documents(self, mock_apply_async):
        mock_apply_async.return_value.id = 'task-123'
        self.deal.source_onedrive_id = 'old-folder'
        self.deal.source_drive_id = 'old-drive'
        self.deal.save(update_fields=['source_onedrive_id', 'source_drive_id'])
        old_doc = DealDocument.objects.create(
            deal=self.deal,
            title='Old pitch.pdf',
            onedrive_id='old-file',
        )
        old_chunk = DocumentChunk.objects.create(
            deal=self.deal,
            source_type='document',
            source_id=str(old_doc.id),
            content='Old pitch deck chunk',
        )

        response = self.client.post(
            f'/api/deals/{self.deal.id}/connect_onedrive/',
            {'source_onedrive_id': 'new-folder', 'source_drive_id': 'new-drive'},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.deal.refresh_from_db()
        self.assertEqual(self.deal.source_onedrive_id, 'new-folder')
        self.assertEqual(self.deal.source_drive_id, 'new-drive')
        self.assertEqual(self.deal.processing_status, 'processing')
        self.assertFalse(DealDocument.objects.filter(id=old_doc.id).exists())
        self.assertFalse(DocumentChunk.objects.filter(id=old_chunk.id).exists())
        mock_apply_async.assert_called_once()
