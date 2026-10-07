from django.test import SimpleTestCase
from ai_orchestrator.services.token_budget import estimate_tokens, estimate_message_tokens, report_evidence_budget

class ReportContextBudgetTests(SimpleTestCase):
    def test_retrieval_counts_escaped_saved_cell_json(self):
        text = '{"period":"FY26E","formula":"=SUM(A1:A3)"}\n' * 1000
        self.assertGreater(estimate_message_tokens(text), estimate_tokens(text))

    def test_h100_budget_reserves_prompt_calculator_output_and_prior_sections(self):
        args = dict(context_window=131072, input_budget=90112, output_budget=16384, evidence_budget=81920)
        budget = report_evidence_budget(**args)
        self.assertEqual(budget, 73728)
        self.assertLess(budget + 24576 + 16384 + 4096, 131072)
        self.assertLess(report_evidence_budget(**args, extra_context='Earlier report ' * 3000), budget)

    def test_smaller_windows_and_input_limits_are_respected(self):
        budget = report_evidence_budget(context_window=65536, input_budget=40960,
            output_budget=16384, evidence_budget=36000)
        self.assertEqual(budget, 20480)
