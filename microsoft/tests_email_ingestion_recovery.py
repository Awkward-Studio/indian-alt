from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from deals.models import Deal, DealDocument
from microsoft.models import Email, EmailAccount, EmailIngestionRun
from microsoft.services.email_evidence import EmailEvidenceService as Evidence
from microsoft.services.email_ingestion import EmailIngestionService as Ingestion


class EmailRecoveryTests(TestCase):
    def setUp(self):
        account = EmailAccount.objects.create(email='recovery@example.test')
        self.deal = Deal.objects.create(title='Recovery company')
        self.email = Email.objects.create(email_account=account, graph_id='recover',
            subject='deal_name=Recovery company', body_text='Revised financing is 140 crore.')

    @patch.object(Ingestion, 'index_outputs', return_value=False)
    def test_vm_off_saves_link_and_body_then_resumes(self, index):
        run = Evidence.snapshot(self.email)
        result = Ingestion.process(run.id, use_ai=False)
        self.assertEqual(result['status'], 'waiting_service')
        self.assertEqual(DealDocument.objects.count(), 1)
        self.email.refresh_from_db()
        self.assertEqual(self.email.deal, self.deal)
        EmailIngestionRun.objects.filter(pk=run.id).update(next_attempt_at=None)
        index.return_value = True
        self.assertEqual(Ingestion.process(run.id, use_ai=False)['status'], 'completed')
        self.assertEqual(DealDocument.objects.count(), 1)

    def test_live_lease_rejects_duplicate_worker(self):
        run = Evidence.snapshot(self.email)
        self.assertIsNotNone(Ingestion.claim(run.id))
        self.assertIsNone(Ingestion.claim(run.id))

    @patch.object(Ingestion, 'index_outputs', return_value=True)
    def test_expired_lease_resumes_and_new_version_still_processes(self, index):
        run = Evidence.snapshot(self.email)
        EmailIngestionRun.objects.filter(pk=run.id).update(status='running', lease_until=timezone.now() - timedelta(seconds=1))
        self.assertEqual(Ingestion.process(run.id, use_ai=False)['status'], 'completed')
        self.email.body_text = 'Updated financing is now 160 crore.'
        self.email.save()
        new = Evidence.snapshot(self.email)
        self.assertNotEqual(new.id, run.id)
        self.assertEqual(Ingestion.process(new.id, use_ai=False)['status'], 'completed')

    @override_settings(EMAIL_INGESTION_ENABLED=True)
    @patch('microsoft.tasks.ingest_email_evidence.apply_async', side_effect=RuntimeError('broker down'))
    def test_broker_outage_keeps_durable_pending_run(self, enqueue):
        with self.captureOnCommitCallbacks(execute=True):
            run = Ingestion.enqueue(self.email)
        self.assertEqual(EmailIngestionRun.objects.get(pk=run.id).status, 'pending')
        enqueue.side_effect = None
        self.assertEqual(Ingestion.reconcile(), 1)

    @patch.object(Ingestion, 'index_outputs', return_value=True)
    def test_manual_classification_survives_retry(self, index):
        run = Evidence.snapshot(self.email)
        run.classification = {'type': 'MEETING_NOTE', 'method': 'manual', 'status': 'completed'}
        run.save()
        Ingestion.process(run.id, use_ai=False)
        self.assertEqual(self.deal.meeting_notes.count(), 1)
