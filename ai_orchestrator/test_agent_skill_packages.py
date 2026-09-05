from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase

from ai_orchestrator.agents.skill_packages import validate_skill_package
from ai_orchestrator.models import AISkill, AISkillRevision


def manifest():
    return {
        "schema_version": "agent_skill_v1",
        "slug": "competitor_research",
        "description": "Compare an authorized deal with cited competitors.",
        "capabilities": ["web.search", "documents.search", "documents.search"],
        "risk": "read_only",
        "compatibility": {"runtime": "pydantic_ai", "min_runtime_version": "2.38.0"},
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
        "references": [{"path": "references/method.md"}],
    }


class AgentSkillPackageTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="skill-owner")
        self.skill = AISkill.objects.create(
            name="package_contract_test",
            prompt_template="Legacy field retained",
            skill_format=AISkill.Format.AGENT_SKILL_V1,
        )

    def test_equivalent_packages_have_stable_digest(self):
        files = {"references/method.md": "Method", "SKILL.md": "Instructions"}
        first = validate_skill_package(manifest(), files)
        reordered = validate_skill_package(
            {**manifest(), "capabilities": ["documents.search", "web.search"]},
            {"SKILL.md": "Instructions", "references/method.md": "Method"},
        )
        self.assertTrue(first.valid)
        self.assertEqual(first.digest, reordered.digest)

    def test_invalid_manifest_cannot_be_published(self):
        revision = AISkillRevision(
            skill=self.skill,
            revision=1,
            status=AISkillRevision.Status.PUBLISHED,
            skill_format=AISkill.Format.AGENT_SKILL_V1,
            package_manifest={"schema_version": "wrong"},
        )
        with self.assertRaises(ValidationError):
            revision.save()
        self.assertFalse(AISkillRevision.objects.filter(skill=self.skill).exists())

    def test_legacy_revision_remains_package_free_and_audit_link_is_unchanged(self):
        legacy = AISkill.objects.create(name="legacy", prompt_template="Prompt")
        revision = AISkillRevision.objects.create(
            skill=legacy,
            revision=1,
            status=AISkillRevision.Status.PUBLISHED,
            prompt_template="Prompt",
        )
        self.assertEqual(revision.package_manifest, {})
        self.assertEqual(revision.package_digest, "")
        self.assertEqual(revision.compatibility_status, "not_applicable")
