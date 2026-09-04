from django.contrib.auth.models import AnonymousUser, User
from django.test import TestCase

from accounts.models import Profile
from ai_orchestrator.agents import (
    AgentAuthorizationError,
    AgentAuthorizationService,
)
from ai_orchestrator.models import AIAuditLog, AIConversation
from deals.models import Deal


class AgentAuthorizationServiceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="analyst")
        self.profile = Profile.objects.create(
            user=self.user,
            email="analyst@example.com",
        )
        self.other_user = User.objects.create_user(username="other")
        self.other_profile = Profile.objects.create(
            user=self.other_user,
            email="other@example.com",
        )
        self.allowed = Deal.objects.create(title="Assigned deal")
        self.allowed.responsibility.add(self.profile)
        self.forbidden = Deal.objects.create(title="Another analyst's deal")
        self.forbidden.responsibility.add(self.other_profile)
        self.audit = self._audit(self.user)

    @staticmethod
    def _audit(user):
        return AIAuditLog.objects.create(
            source_type="agent",
            requested_by=user,
            model_used="gemma-4-12b-it-q8",
            system_prompt="",
            user_prompt="",
            raw_response="",
        )

    def test_builds_scope_from_authenticated_user_responsibility(self):
        dependencies = AgentAuthorizationService.build_dependencies(
            user=self.user,
            audit_log_id=self.audit.id,
            requested_deal_ids=[self.allowed.id],
            requested_capability_ids=["deals.read", "documents.search"],
        )

        self.assertEqual(dependencies.requested_by_id, self.user.id)
        self.assertEqual(dependencies.allowed_deal_ids, {self.allowed.id})
        self.assertEqual(
            dependencies.capability_ids,
            {"deals.read", "documents.search"},
        )

    def test_rejects_cross_user_deal_and_mixed_scope_requests(self):
        for requested in ([self.forbidden.id], [self.allowed.id, self.forbidden.id]):
            with self.subTest(requested=requested):
                with self.assertRaisesRegex(AgentAuthorizationError, "outside"):
                    AgentAuthorizationService.build_dependencies(
                        user=self.user,
                        audit_log_id=self.audit.id,
                        requested_deal_ids=requested,
                        requested_capability_ids=["deals.read"],
                    )

    def test_rejects_anonymous_user_and_another_users_audit(self):
        with self.assertRaises(AgentAuthorizationError):
            AgentAuthorizationService.build_dependencies(
                user=AnonymousUser(),
                audit_log_id=self.audit.id,
            )
        with self.assertRaisesRegex(AgentAuthorizationError, "audit"):
            AgentAuthorizationService.build_dependencies(
                user=self.other_user,
                audit_log_id=self.audit.id,
            )

    def test_rejects_cross_user_conversation_and_unsupported_capability(self):
        conversation = AIConversation.objects.create(user=self.other_user)
        with self.assertRaisesRegex(AgentAuthorizationError, "conversation"):
            AgentAuthorizationService.build_dependencies(
                user=self.user,
                audit_log_id=self.audit.id,
                conversation_id=conversation.id,
            )
        with self.assertRaisesRegex(AgentAuthorizationError, "Unsupported"):
            AgentAuthorizationService.build_dependencies(
                user=self.user,
                audit_log_id=self.audit.id,
                requested_capability_ids=["shell.execute"],
            )

    def test_admin_can_authorize_requested_existing_deals(self):
        admin = User.objects.create_user(username="admin", is_staff=True)
        audit = self._audit(admin)

        dependencies = AgentAuthorizationService.build_dependencies(
            user=admin,
            audit_log_id=audit.id,
            requested_deal_ids=[self.allowed.id, self.forbidden.id],
            requested_capability_ids=["deals.read"],
        )

        self.assertEqual(
            dependencies.allowed_deal_ids,
            {self.allowed.id, self.forbidden.id},
        )
