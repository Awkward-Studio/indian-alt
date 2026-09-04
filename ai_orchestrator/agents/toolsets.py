from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any
from uuid import UUID

from django.db.models import Q
from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import RunContext
from pydantic_ai.toolsets.function import FunctionToolset

from ai_orchestrator.models import DocumentChunk
from deals.models import Deal

from .contracts import AgentDependencies
from .registry import AgentCapabilityRegistry
from .scopes import authorized_chunks, authorized_deals, scoped_deal_ids


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DealHandle(ToolResult):
    source_handle: str
    deal_id: UUID
    title: str
    industry: str = ""
    sector: str = ""
    current_phase: str = ""
    summary: str = ""


class DealSearchPage(ToolResult):
    query: str
    deals: tuple[DealHandle, ...] = ()
    truncated: bool = False


class EvidenceHandle(ToolResult):
    source_handle: str
    deal_id: UUID
    deal_title: str
    source_type: str
    source_id: str
    source_title: str
    excerpt: str
    citation: str


class EvidencePage(ToolResult):
    query: str
    evidence: tuple[EvidenceHandle, ...] = ()
    truncated: bool = False


def _terms(query: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"[a-z0-9][a-z0-9&.-]{2,}", query.lower())))[:12]


def _trim(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else f"{text[: limit - 3].rstrip()}..."


def _fit(items: Iterable[ToolResult], max_chars: int) -> tuple[tuple[Any, ...], bool]:
    fitted: list[ToolResult] = []
    used = 2
    truncated = False
    for item in items:
        size = len(json.dumps(item.model_dump(mode="json"), ensure_ascii=False)) + 1
        if used + size > max_chars:
            truncated = True
            break
        fitted.append(item)
        used += size
    return tuple(fitted), truncated


def _item_budget(empty_page: ToolResult, max_chars: int) -> int:
    envelope_size = len(json.dumps(empty_page.model_dump(mode="json"), ensure_ascii=False))
    return max(0, max_chars - envelope_size + 2)


def search_authorized_deals(
    dependencies: AgentDependencies,
    *,
    query: str,
    deal_ids: Iterable[UUID] | None = None,
    limit: int = 6,
) -> DealSearchPage:
    """Search compact deal metadata without leaving the server-authorized scope."""

    query = _trim(query, 500)
    selected_ids = scoped_deal_ids(dependencies, deal_ids)
    queryset = authorized_deals(dependencies).filter(id__in=selected_ids)
    filters = Q()
    for term in _terms(query):
        filters |= (
            Q(title__icontains=term)
            | Q(industry__icontains=term)
            | Q(sector__icontains=term)
            | Q(deal_summary__icontains=term)
        )
    if filters:
        queryset = queryset.filter(filters)
    rows = queryset.only(
        "id", "title", "industry", "sector", "current_phase", "deal_summary"
    ).order_by("title", "id")[: max(1, min(int(limit), 12))]
    handles = (
        DealHandle(
            source_handle=f"deal:{deal.id}",
            deal_id=deal.id,
            title=_trim(deal.title or "Untitled deal", 300),
            industry=_trim(deal.industry, 150),
            sector=_trim(deal.sector, 150),
            current_phase=_trim(deal.current_phase, 80),
            summary=_trim(deal.deal_summary, 700),
        )
        for deal in rows
    )
    empty_page = DealSearchPage(query=query)
    fitted, truncated = _fit(
        handles,
        _item_budget(empty_page, dependencies.tool_result_max_chars),
    )
    return DealSearchPage(query=query, deals=fitted, truncated=truncated)


def retrieve_authorized_evidence(
    dependencies: AgentDependencies,
    *,
    query: str,
    deal_ids: Iterable[UUID],
    limit: int = 12,
) -> EvidencePage:
    """Retrieve bounded excerpts for explicitly requested, authorized deals."""

    query = _trim(query, 500)
    selected_ids = scoped_deal_ids(dependencies, deal_ids)
    queryset = (
        authorized_chunks(dependencies)
        .filter(deal_id__in=selected_ids)
        .exclude(content="")
        .select_related("deal")
    )
    filters = Q()
    for term in _terms(query):
        filters |= Q(content__icontains=term) | Q(search_text__icontains=term)
    if filters:
        queryset = queryset.filter(filters)
    rows = queryset.only(
        "id", "deal_id", "deal__title", "source_type", "source_id", "content", "metadata"
    ).order_by("-created_at", "id")[: max(1, min(int(limit), 20))]

    def handles():
        for chunk in rows:
            metadata = chunk.metadata if isinstance(chunk.metadata, dict) else {}
            source_title = _trim(
                metadata.get("title")
                or metadata.get("filename")
                or metadata.get("document_name")
                or chunk.source_type,
                300,
            )
            deal_title = _trim(chunk.deal.title or "Untitled deal", 300)
            yield EvidenceHandle(
                source_handle=f"chunk:{chunk.id}",
                deal_id=chunk.deal_id,
                deal_title=deal_title,
                source_type=chunk.source_type,
                source_id=_trim(chunk.source_id, 255),
                source_title=source_title,
                excerpt=_trim(chunk.content, 900),
                citation=f"{deal_title} | {source_title} | chunk:{chunk.id}",
            )

    empty_page = EvidencePage(query=query)
    fitted, truncated = _fit(
        handles(),
        _item_budget(empty_page, dependencies.tool_result_max_chars),
    )
    return EvidencePage(query=query, evidence=fitted, truncated=truncated)


def build_deal_read_toolset(_dependencies: AgentDependencies) -> FunctionToolset[AgentDependencies]:
    toolset: FunctionToolset[AgentDependencies] = FunctionToolset(id="deals.read")

    def search_deals(
        ctx: RunContext[AgentDependencies],
        query: str,
        deal_ids: list[UUID] | None = None,
        limit: int = 6,
    ) -> DealSearchPage:
        """Search deal metadata within the caller's authorized scope."""

        return search_authorized_deals(
            ctx.deps, query=query, deal_ids=deal_ids, limit=limit
        )

    toolset.add_function(search_deals, takes_ctx=True)
    return toolset


def build_document_search_toolset(
    _dependencies: AgentDependencies,
) -> FunctionToolset[AgentDependencies]:
    toolset: FunctionToolset[AgentDependencies] = FunctionToolset(id="documents.search")

    def retrieve_deal_evidence(
        ctx: RunContext[AgentDependencies],
        query: str,
        deal_ids: list[UUID],
        limit: int = 12,
    ) -> EvidencePage:
        """Retrieve compact, citable excerpts for explicitly selected deals."""

        return retrieve_authorized_evidence(
            ctx.deps, query=query, deal_ids=deal_ids, limit=limit
        )

    toolset.add_function(retrieve_deal_evidence, takes_ctx=True)
    return toolset


def build_default_capability_registry() -> AgentCapabilityRegistry:
    registry = AgentCapabilityRegistry()
    registry.register("deals.read", build_deal_read_toolset)
    registry.register("documents.search", build_document_search_toolset)
    return registry
