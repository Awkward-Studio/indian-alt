from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from ai_orchestrator.models import AIAuditLog
from deals.models import Deal
from microsoft.models import Email, EmailAccount, EmailIngestionRun


@override_settings(VLLM_BASE_URL='http://inference.test:8080/v1')
class AuditTaskControlTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('audit-admin', password='test-only', is_staff=True)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_audit_detail_includes_worker_progress_and_related_child_logs(self):
        parent = AIAuditLog.objects.create(
            source_type='deal_full_synthesis', model_used='test',
            system_prompt='test', user_prompt='test', status='PROCESSING',
            celery_task_id='report-task',
            worker_logs=['Reading document section 44 of 51'],
        )
        child = AIAuditLog.objects.create(
            source_type='email_report_section', model_used='test',
            system_prompt='test', user_prompt='test', status='PROCESSING',
            celery_task_id='report-task', context_label='Email report section: Executive Summary',
            worker_logs=['Model request prepared; waiting for inference admission.'],
        )

        response = self.client.get(f'/api/ai/history/{parent.id}/')

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['worker_logs'], ['Reading document section 44 of 51'])
        self.assertEqual(len(response.data['child_audits']), 1)
        self.assertEqual(response.data['child_audits'][0]['id'], str(child.id))
        self.assertEqual(
            response.data['child_audits'][0]['worker_logs'],
            ['Model request prepared; waiting for inference admission.'],
        )

    def test_audit_list_keeps_child_logs_out_of_ledger_payload(self):
        AIAuditLog.objects.create(
            source_type='deal_full_synthesis', model_used='test',
            system_prompt='test', user_prompt='test', status='PROCESSING',
            celery_task_id='report-task',
        )

        response = self.client.get('/api/ai/history/')

        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn('child_audits', response.data['results'][0])

    @patch('ai_orchestrator.views.AIAuditLogViewSet._revoke', return_value=[])
    @patch('ai_orchestrator.views.requests.get')
    def test_clear_active_fails_durable_work_even_without_slots(self, get_slots, _revoke):
        get_slots.return_value.json.return_value = []
        get_slots.return_value.raise_for_status.return_value = None
        deal = Deal.objects.create(title='Active VDR', processing_status='processing')
        account = EmailAccount.objects.create(email='active@example.test')
        email = Email.objects.create(
            email_account=account, graph_id='active', processing_status='processing',
        )
        run = EmailIngestionRun.objects.create(
            email=email, input_version='active-version', status='running',
        )
        audit = AIAuditLog.objects.create(
            source_type='vdr_indexing', source_id=str(deal.id),
            model_used='test', system_prompt='test', user_prompt='test',
            status='PROCESSING', is_success=False, celery_task_id='task-1',
        )

        response = self.client.post('/api/ai/history/clear-active/', {}, format='json')

        self.assertEqual(response.status_code, 200, response.data)
        audit.refresh_from_db()
        deal.refresh_from_db()
        email.refresh_from_db()
        run.refresh_from_db()
        self.assertEqual(audit.status, 'FAILED')
        self.assertEqual(deal.processing_status, 'failed')
        self.assertEqual(email.processing_status, 'idle')
        self.assertEqual(run.status, 'failed')

    @patch('ai_orchestrator.views.VMControlService')
    @patch('ai_orchestrator.views.requests.post')
    @patch('ai_orchestrator.views.requests.get')
    @patch('ai_orchestrator.views.AIAuditLogViewSet._revoke', return_value=[])
    def test_clear_active_restarts_text_container_for_decoding_slot(
        self, _revoke, get_slots, post_slot, vm_service_class,
    ):
        get_slots.return_value.json.return_value = [
            {'id': 0, 'id_task': 74751, 'is_processing': True},
        ]
        get_slots.return_value.raise_for_status.return_value = None
        post_slot.return_value.raise_for_status.return_value = None
        vm_service_class.return_value.restart_text_inference.return_value = {
            'status': 'submitted',
            'container': 'vllm-text-t4',
        }

        response = self.client.post('/api/ai/history/clear-active/', {}, format='json')

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['erased_slot_count'], 0)
        self.assertEqual(
            response.data['processing_slots_before_clear'],
            [{'id': 0, 'id_task': 74751}],
        )
        self.assertEqual(
            response.data['inference_restart'],
            {'status': 'submitted', 'container': 'vllm-text-t4'},
        )
        vm_service_class.return_value.restart_text_inference.assert_called_once_with()

    @patch('ai_orchestrator.views.VMControlService')
    @patch('ai_orchestrator.views.requests.get')
    @patch('ai_orchestrator.views.AIAuditLogViewSet._revoke', return_value=[])
    def test_clear_active_force_restart_releases_processing_without_visible_slot(
        self, _revoke, get_slots, vm_service_class,
    ):
        get_slots.return_value.json.return_value = []
        get_slots.return_value.raise_for_status.return_value = None
        vm_service_class.return_value.restart_text_inference.return_value = {
            'status': 'submitted',
            'container': 'vllm-text-t4',
        }

        response = self.client.post(
            '/api/ai/history/clear-active/',
            {'force_restart': True},
            format='json',
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['inference_restart']['status'], 'submitted')
        vm_service_class.return_value.restart_text_inference.assert_called_once_with()

    @patch('ai_orchestrator.services.inference_queue.cache')
    @patch('ai_orchestrator.views.requests.get')
    @patch('ai_orchestrator.views.AIAuditLogViewSet._revoke', return_value=[])
    def test_clear_active_releases_stale_inference_lease(self, _revoke, get_slots, queue_cache):
        get_slots.return_value.json.return_value = []
        get_slots.return_value.raise_for_status.return_value = None
        queue_cache.get.return_value = {'lease_token': 'stale-token'}
        queue_cache.delete.return_value = True

        response = self.client.post('/api/ai/history/clear-active/', {}, format='json')

        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data['inference_lease_released'])
        queue_cache.delete.assert_called_once_with('ai:inference:lease:v1')
