from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from ai_orchestrator.services.ai_processor import AIProcessorService
from ai_orchestrator.services.inference_queue import InferenceQueueLease
from ai_orchestrator.services.inference_tunnel import build_command


class InferenceTunnelTests(SimpleTestCase):
    def test_forwarding_requires_pinned_host_keys_and_loopback_bindings(self):
        command = build_command({"INFERENCE_SSH_HOST": "h100.example", "INFERENCE_SSH_USER": "report"}, "/private/key", "/private/known_hosts")
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertIn("ExitOnForwardFailure=yes", command)
        self.assertIn("UserKnownHostsFile=/private/known_hosts", command)
        self.assertEqual([command[i + 1] for i, value in enumerate(command) if value == "-L"], [
            "127.0.0.1:8002:127.0.0.1:8002", "127.0.0.1:8081:127.0.0.1:8081", "127.0.0.1:8082:127.0.0.1:8082",
        ])
        self.assertEqual(command[-1], "report@h100.example")

    def test_invalid_port_or_missing_target_fails_before_starting_ssh(self):
        for env in ({}, {"INFERENCE_SSH_HOST": "h100", "INFERENCE_SSH_USER": "report", "INFERENCE_SSH_FORWARD_PORTS": "65536"}):
            with self.assertRaises(ValueError):
                build_command(env, "/key", "/hosts")

    @override_settings(AI_SLOT_TRANSPORT_ENABLED=False)
    def test_vllm_never_requests_llama_slots(self):
        for source_type in InferenceQueueLease.SLOT_MONITORED_SOURCE_TYPES:
            self.assertFalse(InferenceQueueLease.uses_slot_transport(source_type))

    @override_settings(AI_SLOT_TRANSPORT_ENABLED=True)
    def test_t4_retains_slot_watchdog(self):
        self.assertTrue(InferenceQueueLease.uses_slot_transport("vdr_report_section"))

    @override_settings(AI_SLOT_TRANSPORT_ENABLED=False)
    @patch("ai_orchestrator.services.ai_processor.broadcast_audit_log_update")
    @patch("ai_orchestrator.services.ai_processor.InferenceQueueLease")
    def test_vllm_report_uses_normal_transport_and_keeps_queue_lease(self, lease_class, _broadcast):
        audit = MagicMock(source_type="vdr_report_section", source_metadata={}, status="PROCESSING", skill=None, user_prompt="", context_label="Report")
        lease_class.return_value.__enter__.return_value.uses_slot_transport.return_value = False
        service = AIProcessorService.__new__(AIProcessorService)
        service.current_provider = MagicMock()
        service.current_provider.execute_standard.return_value = {"response": "Report section", "raw": {"choices": [{"finish_reason": "stop"}]}}
        service._standard_response({"_serialize_inference": True, "_request_timeout": 1800}, audit, "markdown")
        lease_class.assert_called_once_with(audit, max_wait_seconds=0)
        service.current_provider.execute_standard.assert_called_once_with({}, timeout=1800)
