import unittest
from ai_orchestrator.services.deal_visuals import apply_deal_visual_contract, DEAL_VISUAL_OUTPUT_CONTRACT


class DealVisualContractTests(unittest.TestCase):
    def test_configured_prompt_kept_with_mandatory_rendering_contract(self):
        custom = "Use tables for all numeric comparisons."
        result = apply_deal_visual_contract(custom, "deal_chat", "answer")
        self.assertTrue(result.startswith(custom))
        self.assertTrue(result.endswith(DEAL_VISUAL_OUTPUT_CONTRACT))
        self.assertIn("even when the user does not explicitly say chart", result)
        self.assertIn("Respect an explicit request for a table", result)
        self.assertIn('"description"', result)
        self.assertIn('"values"', result)

    def test_other_stages_unchanged(self):
        for pipeline, stage in [("universal_chat", "answer"), ("deal_chat", "planner"), (None, None)]:
            self.assertEqual(apply_deal_visual_contract("original", pipeline, stage), "original")
