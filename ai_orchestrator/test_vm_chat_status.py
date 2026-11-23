from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from .services.vm_service import VMControlSnapshot
from .vm_status_view import VMStatusView


class VMChatStatusTests(SimpleTestCase):
    def get_status(self, text_state):
        request = APIRequestFactory().get("/api/ai/vm/status/")
        force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
        snapshot = VMControlSnapshot(
            control_enabled=True,
            configured=True,
            target_label="Test inference VM",
            power_state="running",
            services={"text": text_state},
        )
        with (
            patch("ai_orchestrator.vm_status_view.VMControlService") as vm,
            patch("ai_orchestrator.vm_status_view.VLLMProviderService") as provider,
        ):
            vm.return_value.snapshot.return_value = snapshot
            provider.return_value.get_available_models.return_value = ["example-b", "example-a"]
            result = VMStatusView.as_view()(request)
        self.assertEqual(result.status_code, 200)
        return result.data, provider

    def test_retrieves_live_model_names_when_text_is_ready(self):
        data, provider = self.get_status("ready")
        self.assertEqual(data["loaded_models"], ["example-a", "example-b"])
        provider.return_value.get_available_models.assert_called_once_with()

    def test_skips_model_lookup_when_text_is_still_loading(self):
        data, provider = self.get_status("loading")
        self.assertEqual(data["loaded_models"], [])
        provider.assert_not_called()
