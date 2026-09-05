from django.test import SimpleTestCase

from ai_orchestrator.agents.first_party_skills import FIRST_PARTY_SKILL_EVALUATIONS, FIRST_PARTY_SKILL_PACKAGES
from ai_orchestrator.agents.skill_packages import validate_skill_package


class FirstPartyAgentSkillTests(SimpleTestCase):
    def test_all_four_packages_validate_and_require_citations(self):
        self.assertEqual(set(FIRST_PARTY_SKILL_PACKAGES), {
            "competitor_research", "market_news_research", "diligence_review", "investment_memo",
        })
        for slug, package in FIRST_PARTY_SKILL_PACKAGES.items():
            with self.subTest(slug=slug):
                result = validate_skill_package(package["manifest"], package["files"])
                self.assertTrue(result.valid, result.errors)
                schema_text = str(package["manifest"]["output_schema"])
                self.assertTrue("source" in schema_text or "citation" in schema_text)
                self.assertNotIn("shell.execute", package["manifest"]["capabilities"])

    def test_evaluations_cover_failure_and_adversarial_cases(self):
        cases = {item["case"] for item in FIRST_PARTY_SKILL_EVALUATIONS}
        self.assertTrue({"insufficient_evidence", "conflicting_sources", "stale_news", "inaccessible_deal", "evidence_prompt_injection"} <= cases)
