"""Tests verifying connected emails can be re-processed when a deal is deleted."""
from unittest.mock import patch
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import Profile
from ai_orchestrator.models import DocumentChunk
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
