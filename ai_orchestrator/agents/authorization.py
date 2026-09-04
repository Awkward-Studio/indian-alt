from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from django.contrib.auth.models import AbstractBaseUser

from ai_orchestrator.models import AIAuditLog, AIConversation
from deals.models import Deal

from .contracts import AgentDependencies


class AgentAuthorizationError(PermissionError):
    pass


class AgentAuthorizationService:
    """Build immutable run scope exclusively from authenticated server state."""

    SUPPORTED_CAPABILITIES = frozenset({"deals.read", "documents.search"})

    @classmethod
    def build_dependencies(
        cls,
        *,
        user: AbstractBaseUser,
        audit_log_id: UUID,
        requested_deal_ids: Iterable[UUID] = (),
        requested_capability_ids: Iterable[str] = (),
        conversation_id: UUID | None = None,
    ) -> AgentDependencies:
        if not getattr(user, "is_authenticated", False) or not getattr(user, "id", None):
            raise AgentAuthorizationError("An authenticated user is required.")
        if not AIAuditLog.objects.filter(id=audit_log_id, requested_by_id=user.id).exists():
            raise AgentAuthorizationError("The agent audit record does not belong to this user.")
        if conversation_id and not AIConversation.objects.filter(
            id=conversation_id, user_id=user.id
        ).exists():
            raise AgentAuthorizationError("The agent conversation does not belong to this user.")

        requested_ids = frozenset(requested_deal_ids)
        if len(requested_ids) > 100:
            raise AgentAuthorizationError("At most 100 deals may be authorized for one run.")
        allowed_ids = cls._allowed_deal_ids(user, requested_ids)
        forbidden_ids = requested_ids - allowed_ids
        if forbidden_ids:
            raise AgentAuthorizationError("One or more requested deals are outside the user's scope.")

        requested_capabilities = frozenset(requested_capability_ids)
        unsupported = requested_capabilities - cls.SUPPORTED_CAPABILITIES
        if unsupported:
            raise AgentAuthorizationError(
                f"Unsupported agent capabilities: {', '.join(sorted(unsupported))}."
            )
        return AgentDependencies(
            requested_by_id=user.id,
            allowed_deal_ids=allowed_ids,
            capability_ids=requested_capabilities,
            audit_log_id=audit_log_id,
            conversation_id=conversation_id,
        )

    @staticmethod
    def _allowed_deal_ids(
        user: AbstractBaseUser,
        requested_ids: frozenset[UUID],
    ) -> frozenset[UUID]:
        queryset = Deal.objects.filter(id__in=requested_ids)
        if getattr(user, "is_superuser", False) or getattr(user, "is_staff", False):
            return frozenset(queryset.values_list("id", flat=True))
        profile = getattr(user, "profile", None)
        if profile is None or profile.is_disabled:
            return frozenset()
        if profile.is_admin:
            return frozenset(queryset.values_list("id", flat=True))
        return frozenset(
            queryset.filter(responsibility=profile).values_list("id", flat=True)
        )
