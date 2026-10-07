from types import SimpleNamespace

from django.test import SimpleTestCase, override_settings

from ai_orchestrator.services.inference_target import apply_inference_target
from ai_orchestrator.services.runtime import AIRuntimeService
from ai_orchestrator.services.llm_providers import VLLMProviderService
from ai_orchestrator.services.token_budget import ContextBudgetExceeded


class InferenceTargetTests(SimpleTestCase):
    def profile(self, target):
        prefix = target.upper() + "_"
        return {
            prefix + "VLLM_BASE_URL": "http://" + target + "/v1",
            prefix + "VLLM_TEXT_MODEL": target + "-model",
            prefix + "EMBEDDING_BASE_URL": "http://" + target + ":8081/v1",
            prefix + "RERANKER_BASE_URL": "http://" + target + ":8082",
            prefix + "AI_SLOT_TRANSPORT_ENABLED": "true" if target == "t4" else "false",
            prefix + "AZURE_VM_NAME": target + "-vm",
            prefix + "CHAT_MODEL_CONTEXT_TOKENS": "65536" if target == "t4" else "131072",
            prefix + "VDR_REPORT_SECTION_EVIDENCE_TOKENS": "36000" if target == "t4" else "81920",
            prefix + "VDR_REPORT_SECTION_INPUT_TOKENS": "40960" if target == "t4" else "90112",
            prefix + "VDR_ARTIFACT_SEGMENT_SOURCE_TOKENS": "10000" if target == "t4" else "49152",
        }

    def test_switch_updates_endpoints_model_transport_and_vm_together(self):
        env = {"AI_INFERENCE_TARGET": "h100", **self.profile("h100"), **self.profile("t4"),
               "H100_INFERENCE_SSH_HOST": "private-h100", "H100_VLLM_API_KEY": "h100-key",
               "T4_VLLM_API_KEY": "t4-key", "EMBEDDING_API_KEY": "old-key"}
        apply_inference_target(environ=env)
        self.assertEqual(env["INFERENCE_SSH_HOST"], "private-h100")
        self.assertEqual(env["VLLM_API_KEY"], "h100-key")
        self.assertEqual(env["EMBEDDING_API_KEY"], "")
        self.assertEqual(env["VDR_REPORT_SECTION_EVIDENCE_TOKENS"], "81920")
        self.assertEqual(env["VDR_ARTIFACT_SEGMENT_SOURCE_TOKENS"], "49152")
        env["AI_INFERENCE_TARGET"] = "t4"
        apply_inference_target(environ=env)
        self.assertEqual(env["VLLM_BASE_URL"], "http://t4/v1")
        self.assertEqual(env["VLLM_TEXT_MODEL"], "t4-model")
        self.assertEqual(env["VLLM_PLANNER_MODEL"], "t4-model")
        self.assertEqual(env["AZURE_VM_NAME"], "t4-vm")
        self.assertEqual(env["AI_SLOT_TRANSPORT_ENABLED"], "true")
        self.assertEqual(env["INFERENCE_SSH_HOST"], "")
        self.assertEqual(env["VLLM_API_KEY"], "t4-key")
        self.assertEqual(env["CHAT_MODEL_CONTEXT_TOKENS"], "65536")
        self.assertEqual(env["VDR_REPORT_SECTION_INPUT_TOKENS"], "40960")
        self.assertEqual(env["VDR_ARTIFACT_SEGMENT_SOURCE_TOKENS"], "10000")

    def test_large_request_fits_h100_and_is_rejected_for_t4(self):
        payload = {"model": "test", "prompt": "evidence " * 25000,
                   "options": {"max_tokens": 16384}, "_enforce_context_budget": True}
        provider = VLLMProviderService()
        with override_settings(CHAT_MODEL_CONTEXT_TOKENS=131072):
            self.assertEqual(provider._build_chat_body(payload, stream=False)["max_tokens"], 16384)
        with override_settings(CHAT_MODEL_CONTEXT_TOKENS=65536):
            with self.assertRaises(ContextBudgetExceeded):
                provider._build_chat_body(payload, stream=False)

    def test_invalid_or_incomplete_profile_leaves_environment_untouched(self):
        for env in ({"AI_INFERENCE_TARGET": "bad"}, {"AI_INFERENCE_TARGET": "h100"}):
            before = env.copy()
            with self.assertRaises(ValueError):
                apply_inference_target(environ=env)
            self.assertEqual(env, before)

    def test_unselected_profile_preserves_legacy_settings(self):
        env = {"VLLM_BASE_URL": "existing"}
        self.assertEqual(apply_inference_target(environ=env), "")
        self.assertEqual(env, {"VLLM_BASE_URL": "existing"})

    @override_settings(AI_INFERENCE_TARGET="h100", VLLM_TEXT_MODEL="h100-model")
    def test_personality_cannot_send_t4_model_to_h100(self):
        self.assertEqual(AIRuntimeService.get_text_model(SimpleNamespace(text_model_name="t4-model")), "h100-model")
