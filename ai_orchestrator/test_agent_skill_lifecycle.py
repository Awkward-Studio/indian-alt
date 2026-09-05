from django.contrib.auth.models import User
from django.test import TestCase

from ai_orchestrator.models import AISkill, AISkillLifecycleEvent, AISkillRevision
from ai_orchestrator.services.agent_skill_packages import AgentSkillPackageService, SkillPackageLifecycleError


def package(slug="competitor_research"):
    return {
        "manifest": {
            "schema_version": "agent_skill_v1",
            "slug": slug,
            "description": "Governed research",
            "capabilities": ["deals.read", "documents.search"],
            "risk": "read_only",
            "compatibility": {"runtime": "pydantic_ai", "min_runtime_version": "2.38.0"},
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "references": [],
        },
        "files": {"SKILL.md": "Use authorized evidence only."},
    }


class AgentSkillLifecycleTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="admin", is_staff=True)
        self.analyst = User.objects.create_user(username="analyst")
        self.skill = AISkill.objects.create(name="governed", prompt_template="legacy", status="draft")

    def test_publish_is_atomic_versioned_and_audited(self):
        data = package()
        revision = AgentSkillPackageService.publish(
            skill_id=self.skill.id, expected_version=1, actor=self.admin, **data,
        )
        self.skill.refresh_from_db()
        self.assertEqual(self.skill.version, 1)
        self.assertEqual(revision.status, AISkillRevision.Status.PUBLISHED)
        self.assertTrue(revision.package_digest)
        self.assertTrue(AISkillLifecycleEvent.objects.filter(revision=revision, action="published").exists())

    def test_concurrent_version_and_unsafe_package_are_rejected(self):
        data = package()
        data["manifest"]["capabilities"] = ["shell.execute"]
        with self.assertRaises(SkillPackageLifecycleError):
            AgentSkillPackageService.publish(skill_id=self.skill.id, expected_version=1, actor=self.admin, **data)
        self.assertFalse(self.skill.revisions.exists())
        with self.assertRaises(SkillPackageLifecycleError):
            AgentSkillPackageService.publish(skill_id=self.skill.id, expected_version=9, actor=self.admin, **package())

    def test_rollback_creates_new_revision_and_preserves_history(self):
        first = AgentSkillPackageService.publish(skill_id=self.skill.id, expected_version=1, actor=self.admin, **package())
        second = AgentSkillPackageService.publish(skill_id=self.skill.id, expected_version=1, actor=self.admin, **package("market_news"))
        restored = AgentSkillPackageService.rollback(
            skill_id=self.skill.id, revision_id=first.id, expected_version=2, actor=self.admin,
        )
        self.assertEqual(restored.revision, 3)
        self.assertEqual(restored.package_digest, first.package_digest)
        self.assertEqual(self.skill.revisions.count(), 3)
        event = AISkillLifecycleEvent.objects.get(revision=restored, action="rolled_back")
        self.assertEqual(event.metadata["source_revision_id"], str(first.id))
        second.refresh_from_db()
        self.assertEqual(second.status, AISkillRevision.Status.ARCHIVED)

    def test_non_admin_cannot_publish(self):
        with self.assertRaises(PermissionError):
            AgentSkillPackageService.publish(skill_id=self.skill.id, expected_version=1, actor=self.analyst, **package())
