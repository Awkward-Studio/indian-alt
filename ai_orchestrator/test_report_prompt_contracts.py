from django.test import SimpleTestCase

from ai_orchestrator.prompt_contracts import IC_SECTION_TITLES
from ai_orchestrator.services.bulk_prompt_contracts import (
    CLIENT_CHECKLIST_SECTION_GUIDANCE,
    IC_REPORT_SECTION_DECISION_TESTS,
    IC_REPORT_SECTION_SYSTEM_PROMPT,
    build_ic_report_section_user_template,
)
from ai_orchestrator.services.report_section_evidence import (
    SECTION_RETRIEVAL_TERMS,
    SECTION_RETRIEVAL_TESTS,
    build_section_retrieval_template,
)


class ReportPromptContractsTests(SimpleTestCase):
    def test_all_sections_keep_checklist_detail_and_require_decision_analysis(self):
        self.assertEqual(set(IC_SECTION_TITLES), set(IC_REPORT_SECTION_DECISION_TESTS))
        self.assertEqual(set(IC_SECTION_TITLES), set(SECTION_RETRIEVAL_TESTS))
        for title in IC_SECTION_TITLES:
            with self.subTest(title=title):
                writing = build_ic_report_section_user_template(title)
                retrieval = build_section_retrieval_template(title)
                self.assertIn(CLIENT_CHECKLIST_SECTION_GUIDANCE[title], writing)
                self.assertIn(IC_REPORT_SECTION_DECISION_TESTS[title], writing)
                self.assertIn("cited evidence to interpretation", writing)
                self.assertIn("investment implication", writing)
                self.assertIn(SECTION_RETRIEVAL_TERMS[title], retrieval)
                self.assertIn(SECTION_RETRIEVAL_TESTS[title], retrieval)
                self.assertIn("contrary facts", retrieval)
                self.assertIn("{{ deal_title }}", retrieval)
                self.assertIn("{{ section_title }}", retrieval)
        self.assertIn("Do not output hidden deliberation", IC_REPORT_SECTION_SYSTEM_PROMPT)
