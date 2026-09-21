from unittest.mock import patch
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import Profile
from banks.models import Bank
from contacts.models import Contact
from ai_orchestrator.models import AIAuditLog
from deals.models import Deal, DealDocument
from microsoft.models import (
    Email, EmailAccount, EmailEvidenceLink, EmailContributionOccurrence,
    EmailPrivateBlob,
)
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
    @patch.object(Ingestion, 'process')
    def test_ingestion_request_queues_worker_and_returns_audit_id(self, process, dispatch):
        response = self.client.post(self.url + 'ingestion/', {}, format='json')

        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(response.data['status'], 'pending')
        self.assertTrue(response.data['audit_log_id'])
        process.assert_not_called()
        dispatch.assert_called_once()
        audit = AIAuditLog.objects.get(pk=response.data['audit_log_id'])
        self.assertEqual(audit.source_type, 'email_ingestion')
        self.assertEqual(audit.source_id, str(self.email.id))
        self.assertEqual(audit.status, 'PENDING')

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

    @patch.object(Ingestion, 'dispatch')
    def test_confirmed_review_dispatches_evidence_worker(self, dispatch):
        response = self.client.post(self.url + 'ingestion-confirm/', {
            'run_id': str(self.run.id),
            'expected_revision': self.run.revision,
            'deal_id': str(self.deal.id),
            'classification': 'NORMAL_EMAIL',
        }, format='json')

        self.assertEqual(response.status_code, 200, response.data)
        dispatch.assert_called_once_with(self.run.id, audit_log_id=response.data['audit_log_id'])

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

    def test_reset_returns_email_to_first_manual_stage(self):
        self.run.status = 'waiting_service'
        self.run.lease_token = self.run.id
        self.run.save(update_fields=['status', 'lease_token'])
        self.email.processing_status = 'pending'
        self.email.is_processed = True
        self.email.save(update_fields=['processing_status', 'is_processed'])
        document = DealDocument.objects.create(deal=self.deal, title='Reset evidence')
        link = EmailEvidenceLink.objects.create(
            email_account=self.email.email_account, deal=self.deal,
            source_key='reset-body', kind='email_body', document=document,
        )
        EmailContributionOccurrence.objects.create(
            run=self.run, source_key='body:0', evidence=link,
        )

        response = self.client.post(self.url + 'ingestion-reset/', {}, format='json')

        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(self.email.ingestion_runs.exists())
        self.email.refresh_from_db()
        self.assertEqual(self.email.processing_status, 'idle')
        self.assertFalse(self.email.is_processed)
        self.assertIsNone(self.email.deal_id)
        self.assertFalse(DealDocument.objects.filter(pk=document.pk).exists())

    @patch.object(Ingestion, 'dispatch')
    def test_email_attachment_can_be_rerun_from_deal_document_workspace(self, dispatch):
        self.run.status = 'completed'
        self.run.match = {'status': 'matched', 'deal_id': str(self.deal.id)}
        self.run.stages = {
            'classification': 'completed', 'match': 'matched', 'save': 'completed',
            'artifacts': 'completed', 'chunks': 'completed', 'index': 'completed',
            'deal_fields': 'completed',
        }
        self.run.save(update_fields=['status', 'match', 'stages'])
        document = DealDocument.objects.create(
            deal=self.deal,
            title='Pitch Deck.pdf',
            extracted_text='old partial text',
            normalized_text='old partial text',
            evidence_json={'artifact_status': 'complete'},
            is_indexed=True,
            is_ai_analyzed=True,
            transcription_status='partial',
            chunking_status='chunked',
        )
        blob = EmailPrivateBlob.objects.create(
            email_account=self.email.email_account,
            sha256='a' * 64,
            size=3,
            payload=b'pdf',
        )
        link = EmailEvidenceLink.objects.create(
            email_account=self.email.email_account,
            deal=self.deal,
            source_key='blob:' + blob.sha256,
            kind='email_attachment',
            document=document,
            blob=blob,
            index_status='completed',
        )
        occurrence = EmailContributionOccurrence.objects.create(
            run=self.run,
            source_key='attachment:deck',
            evidence=link,
            blob=blob,
            status='saved',
        )

        with self.captureOnCommitCallbacks(execute=True):
            result = Ingestion.rerun_documents(self.deal, [str(document.id)])

        self.assertEqual(result['status'], 'queued')
        document.refresh_from_db()
        self.run.refresh_from_db()
        link.refresh_from_db()
        self.assertFalse(document.is_indexed)
        self.assertEqual(document.normalized_text, '')
        self.assertEqual(document.transcription_status, 'pending')
        self.assertEqual(link.index_status, 'pending')
        self.assertEqual(self.run.status, 'pending')
        self.assertEqual(self.run.source['_rerun_generation'], 1)
        dispatch.assert_called_once_with(self.run.id)

    @patch('deals.services.deal_synthesis.DealSynthesisService.run')
    @patch.object(Ingestion, 'index_outputs', return_value=True)
    @patch.object(Evidence, 'save_links', return_value=[])
    @patch.object(Evidence, 'save_attachments', return_value=[])
    def test_confirmed_email_fills_fields_after_indexing(
        self, _attachments, _links, _index_outputs, synthesize,
    ):
        analysis = type('Analysis', (), {'id': 'field-analysis'})()
        synthesize.return_value = {
            'analysis': analysis,
            'financial': {'status': 'completed', 'summary': {}, 'warning': None},
        }
        self.run.classification = {
            'type': 'NORMAL_EMAIL',
            'status': 'completed',
            'method': 'manual',
            'segment_roles': [],
        }
        self.run.match = {'status': 'matched', 'deal_id': str(self.deal.id)}
        self.run.stages = {'review': 'confirmed'}
        self.run.save(update_fields=['classification', 'match', 'stages'])

        result = Ingestion.process(self.run.id, use_ai=True)

        self.run.refresh_from_db()
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(self.run.stages['index'], 'completed')
        self.assertEqual(self.run.stages['deal_fields'], 'completed')
        self.assertEqual(self.run.stages['financial_synthesis'], 'completed')
        self.assertEqual(self.run.match['field_synthesis_analysis_id'], 'field-analysis')
        synthesize.assert_called_once()

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
    def test_forwarder_owns_new_deal_and_forwarded_sender_is_primary_contact(self, dispatch):
        admin = self.profile
        forwarder_user = User.objects.create_user(username='sheersha', password='test-only')
        forwarder = Profile.objects.create(
            user=forwarder_user,
            email='sheersha.mathur@india-alt.com',
            name='Sheersha Mathur',
        )
        bank = Bank.objects.create(name='White Owl Ventures')
        contact = Contact.objects.create(
            name='Rajesh Katare',
            email='rajesh@thewhiteowlventures.com',
            bank=bank,
        )
        self.email.deal = None
        self.email.from_email = forwarder.email
        self.email.body_text = (
            'Please create this deal.\n\n'
            'From: Rajesh Katare <\nrajesh@thewhiteowlventures.com\n>\n'
            'Sent: Thursday\nTo: Sheersha Mathur\nSubject: Longway'
        )
        self.email.save(update_fields=['deal', 'from_email', 'body_text'])
        self.run.source = {**self.run.source, 'from_email': self.email.from_email, 'body_text': self.email.body_text}
        self.run.match = {
            'status': 'needs_review',
            'initialization': {'deal_model_data': {'title': 'Longway'}},
        }
        self.run.save(update_fields=['source', 'match'])

        response = self.client.post(self.url + 'ingestion-confirm/', {
            'run_id': str(self.run.id),
            'expected_revision': self.run.revision,
            'create_new_deal': True,
            'new_deal_title': 'Longway',
            'classification': 'NORMAL_EMAIL',
        }, format='json')

        self.assertEqual(response.status_code, 200, response.data)
        self.email.refresh_from_db()
        deal = self.email.deal
        self.assertEqual(list(deal.responsibility.values_list('id', flat=True)), [forwarder.id])
        self.assertNotEqual(list(deal.responsibility.values_list('id', flat=True)), [admin.id])
        self.assertEqual(deal.primary_contact_id, contact.id)
        self.assertEqual(deal.bank_id, bank.id)

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
