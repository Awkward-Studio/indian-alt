from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase

from ai_orchestrator.agents import AgentDependencies, SkillCatalog
from ai_orchestrator.models import AIAuditLog, AISkill
from ai_orchestrator.services.agent_skill_packages import AgentSkillPackageService

from .test_agent_skill_lifecycle import package


class AgentSkillCatalogTests(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_user(username="catalog-admin", is_staff=True)
        self.analyst = User.objects.create_user(username="catalog-analyst")
        self.skill = AISkill.objects.create(name="catalog-skill", prompt_template="legacy", status="draft")
        self.revision = AgentSkillPackageService.publish(
            skill_id=self.skill.id, expected_version=1, actor=self.admin, **package(),
        )
        self.audit = AIAuditLog.objects.create(
            source_type="agent", requested_by=self.analyst, model_used="test",
            system_prompt="", user_prompt="", raw_response="",
        )
        self.dependencies = AgentDependencies(
            requested_by_id=self.analyst.id,
            capability_ids={"deals.read", "documents.search"},
            audit_log_id=self.audit.id,
        )

    def test_discovery_is_metadata_only_permission_filtered_and_provenanced(self):
        catalog = SkillCatalog.for_run(
            actor=self.analyst, dependencies=self.dependencies,
            requested_capabilities={"deals.read", "documents.search"},
            requested_task="competitor",
        )
        summary = catalog.summaries()[0]
        self.assertEqual(summary.slug, "competitor_research")
        self.assertFalse(hasattr(summary, "instructions"))
        self.audit.refresh_from_db()
        self.assertEqual(self.audit.source_metadata["agent_skills"]["discovered"], [str(self.revision.id)])

    def test_loading_is_immutable_and_rejects_stale_published_pointer(self):
        catalog = SkillCatalog.for_run(
            actor=self.analyst, dependencies=self.dependencies,
            requested_capabilities={"deals.read", "documents.search"},
            requested_task="competitor",
        )
        loaded = catalog.load("competitor_research")
        self.assertIn("authorized evidence", loaded.instructions)
        self.revision.status = "archived"
        self.revision.save(update_fields=["status"])
        with self.assertRaises(PermissionError):
            catalog.load("competitor_research")

    def test_identity_or_capability_mismatch_hides_skill(self):
        wrong_deps = self.dependencies.model_copy(update={"requested_by_id": self.admin.id})
        self.assertFalse(SkillCatalog.for_run(
            actor=self.analyst, dependencies=wrong_deps,
            requested_capabilities={"deals.read", "documents.search"},
        ).summaries())
        self.assertFalse(SkillCatalog.for_run(
            actor=self.analyst, dependencies=self.dependencies,
            requested_capabilities={"deals.read"},
        ).summaries())
