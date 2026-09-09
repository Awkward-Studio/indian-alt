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
