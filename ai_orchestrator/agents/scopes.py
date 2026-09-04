from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from django.db.models import QuerySet

from ai_orchestrator.models import DocumentChunk
from deals.models import Deal

from .contracts import AgentDependencies


def scoped_deal_ids(
    dependencies: AgentDependencies,
    requested_ids: Iterable[UUID] | None = None,
) -> frozenset[UUID]:
    """Intersect model-supplied IDs with the immutable server authorization scope."""

    if requested_ids is None:
        return dependencies.allowed_deal_ids
    return frozenset(requested_ids) & dependencies.allowed_deal_ids


def authorized_deals(dependencies: AgentDependencies) -> QuerySet[Deal]:
    """The mandatory starting queryset for every agent deal lookup."""

    return Deal.objects.filter(id__in=dependencies.allowed_deal_ids)


def authorized_chunks(dependencies: AgentDependencies) -> QuerySet[DocumentChunk]:
    """The mandatory starting queryset for every agent evidence lookup."""

    return DocumentChunk.objects.filter(deal_id__in=dependencies.allowed_deal_ids)
