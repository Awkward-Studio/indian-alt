from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from ai_orchestrator.models import AIAuditLog, VMControlOperation
from ai_orchestrator.services.vm_service import VMControlSnapshot
from ai_orchestrator.views import VMControlView


class VMControlViewTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.admin = get_user_model().objects.create_user(
            username="vm-admin",
            email="vm-admin@example.com",
            password="unused",
            is_staff=True,
        )
        self.user = get_user_model().objects.create_user(
            username="vm-user",
            email="vm-user@example.com",
            password="unused",
        )
        cache.clear()

    def service(self, power_state="deallocated", available=True):
        service = MagicMock()
        service.available = available
        service.target_label = "Indian Alt T4 Inference VM"
        service.subscription_id = "subscription-id"
        service.resource_group = "indian-alt-prod_group"
        service.vm_name = "indian-alt-prod"
        service.target_resource_id = "/subscriptions/subscription-id/resourceGroups/indian-alt-prod_group/providers/Microsoft.Compute/virtualMachines/indian-alt-prod"
        service.snapshot.return_value = VMControlSnapshot(
            control_enabled=available,
            configured=available,
            target_label=service.target_label,
            power_state=power_state,
            service_state="offline" if power_state != "running" else "ready",
            services={"text": "offline", "embedding": "offline", "reranker": "offline"},
        )
        return service

    def request(self, method, user, data=None):
        request = getattr(self.factory, method)("/api/ai/vm/control/", data or {}, format="json")
        force_authenticate(request, user=user)
        return VMControlView.as_view()(request)

    def test_non_admin_cannot_read_or_submit(self):
        self.assertEqual(self.request("get", self.user).status_code, 403)
        self.assertEqual(self.request("post", self.user, {"action": "start"}).status_code, 403)

    def test_unknown_action_is_rejected_without_provider_call(self):
        with patch("ai_orchestrator.views.VMControlService") as service_class:
            response = self.request("post", self.admin, {"action": "stop"})
        self.assertEqual(response.status_code, 400)
        service_class.assert_not_called()

    def test_incomplete_configuration_fails_closed(self):
        with patch("ai_orchestrator.views.VMControlService", return_value=self.service(available=False)):
            response = self.request("post", self.admin, {"action": "start"})
        self.assertEqual(response.status_code, 503)

    def test_active_ai_work_blocks_deallocation(self):
        AIAuditLog.objects.create(source_type="test", source_id="active", status="PROCESSING")
        service = self.service(power_state="running")
        with patch("ai_orchestrator.views.VMControlService", return_value=service):
            response = self.request("post", self.admin, {"action": "deallocate"})
        self.assertEqual(response.status_code, 409)
        service.stop_vm.assert_not_called()

    def test_start_submission_is_audited(self):
        service = self.service()
        with patch("ai_orchestrator.views.VMControlService", return_value=service):
            response = self.request("post", self.admin, {"action": "start"})
        self.assertEqual(response.status_code, 202)
        service.start_vm.assert_called_once_with()
        operation = VMControlOperation.objects.get()
        self.assertEqual(operation.action, "start")
        self.assertEqual(operation.status, "submitted")
        self.assertEqual(operation.requested_by, self.admin)
        self.assertEqual(operation.target_resource_id, service.target_resource_id)

    def test_status_poll_marks_submitted_operation_succeeded(self):
        operation = VMControlOperation.objects.create(
            action="start",
            target_label="Indian Alt T4 Inference VM",
            target_vm_name="indian-alt-prod",
            target_resource_id="resource-id",
            requested_by=self.admin,
        )
        with patch("ai_orchestrator.views.VMControlService", return_value=self.service(power_state="running")):
            response = self.request("get", self.admin)
        self.assertEqual(response.status_code, 200)
        operation.refresh_from_db()
        self.assertEqual(operation.status, "succeeded")
        self.assertIsNotNone(operation.completed_at)

    def test_concurrent_submission_returns_conflict(self):
        service = self.service()
        with patch("ai_orchestrator.views.VMControlService", return_value=service), patch(
            "ai_orchestrator.views.cache.add", return_value=False
        ):
            response = self.request("post", self.admin, {"action": "start"})
        self.assertEqual(response.status_code, 409)
        service.start_vm.assert_not_called()
