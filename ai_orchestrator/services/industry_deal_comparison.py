"""Find source-backed internal peers for the IC Industry Overview step."""

from __future__ import annotations

from collections import OrderedDict
from uuid import UUID

from django.db.models import Q

from deals.models import (
    Deal,
    DealDocument,
    DealRelationshipContext,
    VentureIntelligenceCompanyRelation,
)


class IndustryDealComparisonService:
    """Discover database peers, then rank only their indexed source chunks."""

    MAX_SAME_SECTOR_CANDIDATES = 20
    MAX_RANKED_CHUNKS = 160

    def __init__(self, *, deal: Deal, embedding_service):
        self.deal = deal
        self.embedding_service = embedding_service

    @staticmethod
    def _valid_uuid(value) -> str | None:
        try:
            return str(UUID(str(value)))
        except (TypeError, ValueError, AttributeError):
            return None

    def _discover_deals(self) -> OrderedDict[str, tuple[Deal, str]]:
        explicit_ids: set[str] = set()
        for relation in DealRelationshipContext.objects.filter(
            deal=self.deal,
            relationship_type=DealRelationshipContext.RelationshipType.COMPETITOR,
        ):
            if relation.related_deal_id:
                explicit_ids.add(str(relation.related_deal_id))
            explicit_ids.update(
                valid for value in (relation.selected_deal_ids or [])
                if (valid := self._valid_uuid(value))
            )
        explicit_ids.update(
            str(deal_id) for deal_id in DealRelationshipContext.objects.filter(
                related_deal=self.deal,
                relationship_type=DealRelationshipContext.RelationshipType.COMPETITOR,
            ).values_list("deal_id", flat=True)
        )
        explicit_ids.discard(str(self.deal.id))

        names = {
            str(item.get("name") or item.get("company_name") or "").strip()
            for item in (self.deal.competitor_candidates or [])
            if isinstance(item, dict)
        }
        names.update(
            str(relation.company_profile.name or "").strip()
            for relation in VentureIntelligenceCompanyRelation.objects.filter(
                deal=self.deal, relation_type="competitor"
            ).select_related("company_profile")
        )
        named_ids: set[str] = set()
        for name in names:
            if 5 <= len(name) <= 120:
                named_ids.update(
                    str(pk) for pk in Deal.objects.filter(
                        Q(title__iexact=name) | Q(title__icontains=name)
                    ).exclude(pk=self.deal.id).values_list("pk", flat=True)[:5]
                )

        discovered: OrderedDict[str, tuple[Deal, str]] = OrderedDict()
        for peer in Deal.objects.filter(pk__in=explicit_ids | named_ids).order_by("title"):
            relationship = (
                "linked competitor" if str(peer.id) in explicit_ids
                else "named competitor candidate"
            )
            discovered[str(peer.id)] = (peer, relationship)

        sector = str(getattr(self.deal, "sector", "") or "").strip()
        industry = str(getattr(self.deal, "industry", "") or "").strip()
        scope = Q()
        if sector.casefold() not in {"", "other", "unknown", "n/a"}:
            scope |= Q(sector__iexact=sector)
        if industry.casefold() not in {"", "other", "unknown", "n/a"}:
            scope |= Q(industry__iexact=industry)
        if scope:
            for peer in Deal.objects.filter(scope).exclude(pk=self.deal.id).order_by("title")[: self.MAX_SAME_SECTOR_CANDIDATES]:
                discovered.setdefault(str(peer.id), (peer, "same-sector peer candidate"))
        return discovered

    def retrieve(self, query: str) -> dict:
        peers = self._discover_deals()
        source_info: dict[str, dict] = {}
        for document in DealDocument.objects.filter(deal_id__in=peers).only(
            "id", "deal_id", "title", "file_url"
        ):
            peer, relationship = peers[str(document.deal_id)]
            source_info[str(document.id)] = {
                "company": peer.title,
                "deal_id": str(peer.id),
                "relationship": relationship,
                "title": f"{peer.title}: {document.title}",
                "url": document.file_url or "",
                "source_type": "document",
            }

        for relation in VentureIntelligenceCompanyRelation.objects.filter(
            deal=self.deal, relation_type="competitor"
        ).select_related("company_profile"):
            profile = relation.company_profile
            source_info[f"vi_{profile.id}"] = {
                "company": profile.name,
                "deal_id": str(self.deal.id),
                "relationship": "linked competitor profile",
                "title": f"{profile.name}: Venture Intelligence profile",
                "url": "",
                "source_type": "extracted_source",
            }

        if not source_info:
            return {"chunks": [], "source_info": {}, "candidate_deal_count": len(peers)}
        try:
            chunks = self.embedding_service.search_global_chunks(
                query,
                limit=self.MAX_RANKED_CHUNKS,
                deal_ids=[str(self.deal.id), *peers.keys()],
                source_ids=list(source_info),
                rerank=True,
            )
        except Exception:
            try:
                chunks = self.embedding_service.search_global_chunks(
                    query,
                    limit=self.MAX_RANKED_CHUNKS,
                    deal_ids=[str(self.deal.id), *peers.keys()],
                    source_ids=list(source_info),
                    rerank=False,
                )
            except Exception:
                chunks = []
        eligible = [
            chunk for chunk in chunks
            if (source := source_info.get(str(chunk.source_id)))
            and chunk.source_type == source["source_type"]
            and str(chunk.deal_id) == source["deal_id"]
            and str(chunk.content or "").strip()
        ]
        return {
            "chunks": eligible,
            "source_info": source_info,
            "candidate_deal_count": len(peers),
        }
