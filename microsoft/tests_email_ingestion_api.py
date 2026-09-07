from unittest.mock import patch
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from deals.models import Deal, DealDocument
from microsoft.models import Email, EmailAccount, EmailEvidenceLink
from microsoft.services.email_evidence import EmailEvidenceService as Evidence
from microsoft.services.email_ingestion import EmailIngestionService as Ingestion


@override_settings(EMAIL_INGESTION_ENABLED=True)
class EmailIngestionAPITests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='email-review', password='test-only')
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        account = EmailAccount.objects.create(email='review@example.test')
        self.deal = Deal.objects.create(title='Original company')
        self.other = Deal.objects.create(title='Correct company')
        self.email = Email.objects.create(email_account=account, graph_id='review', body_text='Revised terms', deal=self.deal)
        self.run = Evidence.snapshot(self.email)
        self.url = f'/api/microsoft/emails/emails/{self.email.id}/'

    def test_anonymous_cannot_read_status(self):
        self.client.force_authenticate(None)
        self.assertIn(self.client.get(self.url + 'ingestion/').status_code, [401, 403])

    @patch.object(Ingestion, 'dispatch')
    @patch.object(Ingestion, 'index_outputs', return_value=False)
    def test_correction_removes_old_owned_documents_and_rejects_stale_revision(self, index, dispatch):
        Ingestion.process(self.run.id, use_ai=False)
        self.run.refresh_from_db()
        payload = {'run_id': str(self.run.id), 'expected_revision': self.run.revision,
                   'deal_id': str(self.other.id), 'classification': 'NORMAL_EMAIL'}
        response = self.client.post(self.url + 'ingestion-confirm/', payload, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(DealDocument.objects.filter(deal=self.deal).exists())
        self.assertFalse(EmailEvidenceLink.objects.filter(deal=self.deal, active=True).exists())
        self.assertEqual(self.client.post(self.url + 'ingestion-confirm/', payload, format='json').status_code, 409)

    def test_other_emails_run_cannot_be_confirmed(self):
        other_email = Email.objects.create(email_account=self.email.email_account, graph_id='another')
        other_run = Evidence.snapshot(other_email)
        response = self.client.post(self.url + 'ingestion-confirm/', {'run_id': str(other_run.id),
            'expected_revision': 1, 'deal_id': str(self.deal.id)}, format='json')
        self.assertEqual(response.status_code, 404)

    @patch.object(Ingestion, 'index_outputs', return_value=False)
    def test_download_is_scoped_and_private(self, index):
        Ingestion.process(self.run.id, use_ai=False)
        occurrence = self.run.occurrences.get()
        response = self.client.get(self.url + 'evidence-source/', {'run_id': str(self.run.id), 'occurrence_id': str(occurrence.id)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Cache-Control'], 'private, no-store')
        self.assertIn(b'Revised terms', b''.join(response.streaming_content))

    def test_invalid_deal_identifier_is_validation_error(self):
        response = self.client.post(self.url + 'ingestion-confirm/', {'run_id': str(self.run.id),
            'expected_revision': 1, 'deal_id': 'invalid'}, format='json')
        self.assertEqual(response.status_code, 400)
