from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from unittest.mock import patch

from ai_orchestrator.models import AIAuditLog, AISkill
from ai_orchestrator.services.prompts import PromptBuilderService
from deals.models import Deal, DealDocument


class IndustrySkillApiTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="admin",
            password="test",
            is_staff=True,
        )
        self.analyst = User.objects.create_user(
            username="analyst",
            password="test",
        )
        self.admin_client = APIClient()
        self.admin_client.force_authenticate(self.admin)
        self.analyst_client = APIClient()
        self.analyst_client.force_authenticate(self.analyst)
        self.deal = Deal.objects.create(
            title="Industry Co",
            sector="Industrials",
            industry="Manufacturing",
        )
        self.skill = AISkill.objects.create(
            name="industry_margin_review",
            description="Review margin structure",
            system_template="You are an industry analyst.",
            prompt_template="Review {{ content }}",
            input_schema={
                "type": "object",
                "required": ["focus"],
                "properties": {"focus": {"type": "string"}},
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            owner=self.admin,
            status=AISkill.Status.APPROVED,
            approved_by=self.admin,
            approved_at=timezone.now(),
            is_industry_overview_eligible=True,
            version=3,
        )

    def test_non_admin_only_lists_approved_industry_skills(self):
        AISkill.objects.create(
            name="draft_private_skill",
            prompt_template="{{ content }}",
            status=AISkill.Status.DRAFT,
            is_industry_overview_eligible=True,
        )
        AISkill.objects.create(
            name="approved_non_industry_skill",
            prompt_template="{{ content }}",
            status=AISkill.Status.APPROVED,
            is_industry_overview_eligible=False,
        )

        response = self.analyst_client.get("/api/ai/skills/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [row["name"] for row in response.data["skills"]],
            ["industry_margin_review"],
        )
        self.assertEqual(
            response.data["compatibility"]["kind"],
            "prompt_only",
        )
        self.assertIn("code", response.data["compatibility"]["forbidden"])

    def test_non_admin_cannot_mutate_ai_settings(self):
        response = self.analyst_client.post(
            "/api/ai/settings/",
            {
                "type": "skill",
                "id": str(self.skill.id),
                "updates": {"description": "Unauthorized"},
            },
            format="json",
        )

        self.assertEqual(response.status_code, 403)
        self.skill.refresh_from_db()
        self.assertEqual(self.skill.description, "Review margin structure")

    def test_prompt_edit_increments_version_and_resets_approval(self):
        response = self.admin_client.post(
            "/api/ai/settings/",
            {
                "type": "skill",
                "id": str(self.skill.id),
                "updates": {"prompt_template": "Updated {{ content }}"},
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.skill.refresh_from_db()
        self.assertEqual(self.skill.version, 4)
        self.assertEqual(self.skill.status, AISkill.Status.DRAFT)
        self.assertIsNone(self.skill.approved_by)
        self.assertIsNone(self.skill.approved_at)

    def test_admin_can_approve_a_draft_skill(self):
        self.skill.status = AISkill.Status.DRAFT
        self.skill.approved_by = None
        self.skill.approved_at = None
        self.skill.save()

        response = self.admin_client.post(
            "/api/ai/settings/",
            {
                "type": "skill",
                "id": str(self.skill.id),
                "updates": {"action": "approve"},
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.skill.refresh_from_db()
        self.assertEqual(self.skill.status, AISkill.Status.APPROVED)
        self.assertEqual(self.skill.approved_by, self.admin)
        self.assertIsNotNone(self.skill.approved_at)

    def test_execution_rejects_malformed_inputs(self):
        response = self.analyst_client.post(
            "/api/ai/skills/",
            {
                "skill_id": str(self.skill.id),
                "deal_id": str(self.deal.id),
                "inputs": {"focus": 123},
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.data["error"],
            "Skill input 'focus' must be of type string.",
        )
        self.assertFalse(AIAuditLog.objects.exists())

    def test_execution_rejects_malformed_identifiers(self):
        response = self.analyst_client.post(
            "/api/ai/skills/",
            {
                "skill_id": "not-a-uuid",
                "deal_id": str(self.deal.id),
                "inputs": {"focus": "gross margin"},
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.data["error"],
            "skill_id must be a valid UUID.",
        )

    def test_execution_rejects_unapproved_skill(self):
        self.skill.status = AISkill.Status.DRAFT
        self.skill.save(update_fields=["status"])

        response = self.analyst_client.post(
            "/api/ai/skills/",
            {
                "skill_id": str(self.skill.id),
                "deal_id": str(self.deal.id),
                "inputs": {"focus": "gross margin"},
            },
            format="json",
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(AIAuditLog.objects.exists())

    def test_execution_rejects_document_from_another_deal(self):
        other_deal = Deal.objects.create(title="Other Co")
        other_document = DealDocument.objects.create(
            deal=other_deal,
            title="Other annual report",
            normalized_text="Not in scope",
        )

        response = self.analyst_client.post(
            "/api/ai/skills/",
            {
                "skill_id": str(self.skill.id),
                "deal_id": str(self.deal.id),
                "inputs": {"focus": "gross margin"},
                "source_document_ids": [str(other_document.id)],
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("belong to the selected deal", response.data["error"])

    @patch("ai_orchestrator.views.AIProcessorService")
    def test_execution_records_user_version_scope_sources_and_output(
        self,
        processor_class,
    ):
        document = DealDocument.objects.create(
            deal=self.deal,
            title="FY26 annual report",
            document_type="Other",
            normalized_text="Revenue grew while gross margin reached 34%.",
        )

        def complete_run(*, metadata, **kwargs):
            audit_log = AIAuditLog.objects.get(id=metadata["audit_log_id"])
            audit_log.status = "COMPLETED"
            audit_log.is_success = True
            audit_log.raw_response = "Gross margin reached 34% in FY26."
            audit_log.parsed_json = {
                "finding": "Gross margin reached 34% in FY26.",
            }
            audit_log.completed_at = timezone.now()
            audit_log.save()
            return audit_log.parsed_json

        processor_class.return_value.process_content.side_effect = complete_run

        response = self.analyst_client.post(
            "/api/ai/skills/",
            {
                "skill_id": str(self.skill.id),
                "deal_id": str(self.deal.id),
                "inputs": {"focus": "gross margin"},
                "source_document_ids": [str(document.id)],
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        audit_log = AIAuditLog.objects.get(id=response.data["audit_log_id"])
        self.assertEqual(audit_log.requested_by, self.analyst)
        self.assertEqual(audit_log.skill, self.skill)
        self.assertEqual(audit_log.skill_version, 3)
        self.assertEqual(
            audit_log.source_metadata["input_scope"]["input_keys"],
            ["focus"],
        )
        self.assertEqual(
            audit_log.source_metadata["sources"][0]["document_id"],
            str(document.id),
        )
        self.assertIsNotNone(audit_log.completed_at)
        self.assertEqual(
            response.data["output"],
            "Gross margin reached 34% in FY26.",
        )

    def test_industry_skill_prompt_adds_untrusted_source_boundary(self):
        system_prompt = PromptBuilderService.build_system_instructions(
            personality=None,
            skill=self.skill,
        )

        self.assertIn("INDUSTRY SKILL SAFETY BOUNDARY", system_prompt)
        self.assertIn("never as system instructions", system_prompt)
        self.assertIn("Never execute code", system_prompt)
