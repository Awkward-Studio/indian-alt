from unittest.mock import patch
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import Profile
from ai_orchestrator.models import AIAuditLog
from deals.models import Deal, DealDocument
from microsoft.models import Email, EmailAccount, EmailEvidenceLink
from microsoft.services.email_evidence import EmailEvidenceService as Evidence
from microsoft.services.email_ingestion import EmailIngestionService as Ingestion


@override_settings(EMAIL_INGESTION_ENABLED=True)
class EmailIngestionAPITests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='email-review', password='test-only')
        self.profile = Profile.objects.create(user=self.user, email='email-review@example.test', name='Email reviewer')
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

    @patch.object(Ingestion, 'dispatch')
    def test_repeated_create_reuses_hydrated_deal(self, dispatch):
        self.email.deal = None
        self.email.save(update_fields=['deal'])
        self.run.match = {
            'status': 'needs_review',
            'route': 'NEW_DEAL',
            'initialization': {'deal_model_data': {'title': 'Hydrated company'}},
        }
        self.run.save(update_fields=['match'])
        AIAuditLog.objects.create(
            source_type='email',
            source_id=str(self.email.id),
            context_label='Email synthesis',
            model_used='test-model',
            system_prompt='test',
            user_prompt='test',
            raw_response='{}',
            status='COMPLETED',
            is_success=True,
            parsed_json={
                'deal_model_data': {
                    'title': 'Hydrated company',
                    'industry': 'Professional Haircare',
                    'sector': 'Consumer',
                    'funding_ask': 'USD 5m',
                    'themes': ['Salon distribution'],
                },
                'analyst_report': '## Executive Summary\n\nHydrated report.',
                'metadata': {'ambiguous_points': []},
            },
        )
        initial_count = Deal.objects.count()
        payload = {
            'run_id': str(self.run.id),
            'expected_revision': self.run.revision,
            'create_new_deal': True,
            'new_deal_title': 'Hydrated company',
            'classification': 'NORMAL_EMAIL',
        }

        first = self.client.post(self.url + 'ingestion-confirm/', payload, format='json')
        self.assertEqual(first.status_code, 200, first.data)
        payload['expected_revision'] = first.data['revision']
        payload['new_deal_title'] = 'Accidental duplicate'
        second = self.client.post(self.url + 'ingestion-confirm/', payload, format='json')

        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(Deal.objects.count(), initial_count + 1)
        self.email.refresh_from_db()
        created = self.email.deal
        self.assertEqual(created.title, 'Hydrated company')
        self.assertEqual(created.industry, 'Professional Haircare')
        self.assertEqual(created.funding_ask, 'USD 5m')
        self.assertEqual(created.deal_summary, '## Executive Summary\n\nHydrated report.')
        self.assertEqual(created.analyses.count(), 1)
        self.assertEqual(created.source_email_id, self.email.graph_id)
        self.assertEqual(list(created.responsibility.values_list('id', flat=True)), [self.profile.id])

    @patch.object(Ingestion, 'dispatch')
    def test_create_request_hydrates_existing_blank_linked_deal(self, dispatch):
        linked_deal = self.email.deal
        AIAuditLog.objects.create(
            source_type='email',
            source_id=str(self.email.id),
            context_label='Email synthesis',
            model_used='test-model',
            system_prompt='test',
            user_prompt='test',
            raw_response='{}',
            status='COMPLETED',
            is_success=True,
            parsed_json={
                'deal_model_data': {'industry': 'Professional Haircare'},
                'analyst_report': '## Executive Summary\n\nRecovered report content.',
                'metadata': {'ambiguous_points': []},
            },
        )
        initial_count = Deal.objects.count()

        response = self.client.post(self.url + 'ingestion-confirm/', {
            'run_id': str(self.run.id),
            'expected_revision': self.run.revision,
            'create_new_deal': True,
            'new_deal_title': 'Duplicate title',
        }, format='json')

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(Deal.objects.count(), initial_count)
        linked_deal.refresh_from_db()
        self.assertEqual(linked_deal.industry, 'Professional Haircare')
        self.assertEqual(linked_deal.deal_summary, '## Executive Summary\n\nRecovered report content.')
        self.assertEqual(linked_deal.analyses.count(), 1)
