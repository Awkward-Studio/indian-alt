from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from deals.models import Deal, DealDocument
from microsoft.models import Email, EmailAccount, EmailIngestionRun
from microsoft.services.email_evidence import EmailEvidenceService as Evidence
from microsoft.services.email_ingestion import EmailIngestionService as Ingestion
from microsoft.services.email_matching import EmailDecisionService as Decisions, EmailDecisionUnavailable
from ai_orchestrator.models import AIAuditLog


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

    @override_settings(EMAIL_INGESTION_ENABLED=True)
    @patch.object(Ingestion, '_observed_task_ids', return_value=set())
    @patch('microsoft.services.email_ingestion.cache.get')
    @patch.dict('os.environ', {'RAILWAY_DEPLOYMENT_ID': 'old-deploy'})
    def test_reconcile_recovers_live_lease_from_replaced_worker(self, cache_get, _observed):
        run = Evidence.snapshot(self.email)
        audit = Ingestion.ensure_audit_log(run)
        claimed = Ingestion.claim(run.id)
        audit.celery_task_id = 'old-task'
        audit.status = 'PROCESSING'
        audit.save(update_fields=['celery_task_id', 'status'])
        segment = AIAuditLog.objects.create(
            source_type='document_evidence_segment', source_id=str(self.email.id),
            context_label='Document Evidence: attachment [1/2]', model_used='test',
            system_prompt='test', user_prompt='test', status='PROCESSING',
            celery_task_id='old-task',
        )
        cache_get.return_value = {'instance_id': 'new-deploy', 'started_at': timezone.now().timestamp()}

        with patch.object(Ingestion, 'dispatch') as dispatch:
            recovered = Ingestion.reconcile()

        claimed.refresh_from_db()
        segment.refresh_from_db()
        self.assertEqual(recovered, 1)
        self.assertEqual(claimed.status, 'pending')
        self.assertIsNone(claimed.lease_until)
        self.assertEqual(claimed.source['_deployment_recovery_count'], 1)
        self.assertEqual(segment.status, 'FAILED')
        dispatch.assert_called_once_with(claimed.id)

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

    @patch.object(Ingestion, 'decision_document_context')
    @patch.object(Decisions, 'initialize', return_value={'deal_model_data': {'title': 'WHP Jewellers'}})
    @patch.object(Decisions, 'route')
    def test_review_route_uses_email_body_before_artifact_processing(self, route, initialize, document_context):
        route.return_value = {
            'classification': {
                'type': 'NORMAL_EMAIL', 'status': 'completed', 'segment_roles': [],
            },
            'match': {
                'status': 'needs_review', 'deal_id': None, 'suggested_deal_id': None,
                'candidates': [], 'route': 'REVIEW',
            },
        }
        self.email.attachments = [{
            'id': 'attachment-1',
            'name': 'WHP Jewellers - financial projections - clean.xlsx',
            'size': 71923,
            'isInline': False,
        }]
        self.email.save(update_fields=['attachments'])
        run = Evidence.snapshot(self.email)

        result = Ingestion.process(run.id, use_ai=True)

        self.assertEqual(result['status'], 'needs_review')
        run.refresh_from_db()
        self.assertEqual(run.match['initialization']['deal_model_data']['title'], 'WHP Jewellers')
        initialize.assert_called_once()
        metadata = initialize.call_args.kwargs['supplemental_text']
        self.assertIn('WHP Jewellers - financial projections - clean.xlsx', metadata)
        self.assertIn('filenames and link labels only', metadata)
        document_context.assert_not_called()

    @patch.object(Decisions, 'initialize', return_value={'deal_model_data': {'title': 'Jewellery - Investment opportunity'}})
    def test_filename_hint_replaces_generic_subject_title(self, initialize):
        self.email.subject = 'Fw: Jewellery - Investment opportunity'
        self.email.attachments = [{
            'id': 'attachment-1',
            'name': 'WHP Jewellers - financial projections - clean.xlsx',
        }]
        self.email.save(update_fields=['subject', 'attachments'])
        run = Evidence.snapshot(self.email)

        self.assertEqual(
            Ingestion.prefer_attachment_title(run, initialize.return_value)['deal_model_data']['title'],
            'WHP Jewellers',
        )

    @patch.object(Decisions, 'route', side_effect=EmailDecisionUnavailable('VM request timed out'))
    def test_transient_decision_failure_is_scheduled_for_retry(self, _route):
        run = Evidence.snapshot(self.email)

        result = Ingestion.process(run.id, use_ai=True)

        self.assertEqual(result['status'], 'waiting_service')
        run.refresh_from_db()
        self.assertEqual(run.error, 'VM request timed out')
        self.assertIsNotNone(run.next_attempt_at)
        self.email.refresh_from_db()
        self.assertEqual(self.email.processing_status, 'pending')
