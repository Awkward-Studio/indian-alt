"""Retrieve source-backed internal peers for the IC Industry Overview."""

from __future__ import annotations

import re
from collections import OrderedDict
from uuid import UUID

from django.db.models import Q

from deals.models import Deal, DealDocument, DealRelationshipContext, VentureIntelligenceCompanyRelation


class IndustryDealComparisonService:
    """Rank deal-document passages globally, without an industry gate."""

    MAX_RANKED_CHUNKS_PER_QUERY = 160
    MAX_PROFILE_CHUNKS = 48
    _BOILERPLATE_TITLE = re.compile(r"\b(?:nda|non.disclosure|confidentiality agreement)\b", re.I)
    _BOILERPLATE_CONTENT = re.compile(
        r"\b(?:permitted uses include|keep secret and confidential|"
        r"no representation of accuracy or completeness|"
        r"not to be published, quoted or referred to)\b",
        re.I,
    )
    _QUANTIFIED_BUSINESS_EVIDENCE = re.compile(
        r"\b(?:revenue|sales|gross margin|ebitda|capacity|market share|retention|"
        r"customer|pricing|growth|orders)\b.*\d|\d.*\b(?:revenue|sales|margin|ebitda|"
        r"capacity|market share|retention|customer|pricing|growth|orders)\b",
        re.I,
    )

    def __init__(self, *, deal: Deal, embedding_service):
        self.deal = deal
        self.embedding_service = embedding_service

    @staticmethod
    def _valid_uuid(value) -> str | None:
        try:
            return str(UUID(str(value)))
        except (TypeError, ValueError, AttributeError):
            return None

    def _linked_deals(self) -> OrderedDict[str, str]:
        explicit_ids: set[str] = set()
        for relation in DealRelationshipContext.objects.filter(
            deal=self.deal, relationship_type=DealRelationshipContext.RelationshipType.COMPETITOR,
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
            for item in (self.deal.competitor_candidates or []) if isinstance(item, dict)
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
        return OrderedDict(
            (str(peer.id), "linked competitor" if str(peer.id) in explicit_ids else "named competitor candidate")
            for peer in Deal.objects.filter(pk__in=explicit_ids | named_ids).order_by("title")
        )

    def _target_profile(self) -> str:
        # Business-model text shapes the semantic query. Industry is never a DB filter.
        fields = [
            str(self.deal.title or ""), str(self.deal.sector or ""),
            str(self.deal.company_details or "")[:700],
            str(self.deal.deal_summary or "")[:700],
        ]
        return " ".join(" ".join(value.split()) for value in fields if value).strip()[:1_500]

    def _search(self, query: str, *, limit: int, **filters) -> list:
        try:
            return self.embedding_service.search_global_chunks(query, limit=limit, rerank=True, **filters)
        except Exception:
            try:
                return self.embedding_service.search_global_chunks(query, limit=limit, rerank=False, **filters)
            except Exception:
                return []

    @classmethod
    def _substantive(cls, chunk, title: str) -> bool:
        content = " ".join(str(chunk.content or "").split())
        return (
            (len(content) >= 80 or (len(content) >= 30 and cls._QUANTIFIED_BUSINESS_EVIDENCE.search(content)))
            and not cls._BOILERPLATE_TITLE.search(str(title or ""))
            and not cls._BOILERPLATE_CONTENT.search(content)
        )

    def retrieve(self, query: str) -> dict:
        linked = self._linked_deals()
        profile = self._target_profile()
        queries = [
            f"{profile}. {query}",
            f"{profile}. Comparable companies with overlapping products, buyers, business model, "
            "distribution and geography. Source evidence on pricing, revenue, growth, "
            "gross margin, capacity, customer retention and competitive wins or losses.",
        ]
        ranked: dict[str, dict] = {}
        for search_query in queries:
            chunks = self._search(
                search_query, limit=self.MAX_RANKED_CHUNKS_PER_QUERY,
                source_types=["document"], exclude_deal_ids=[str(self.deal.id)],
            )
            for rank, chunk in enumerate(chunks, start=1):
                if chunk.source_type != "document" or str(chunk.deal_id) == str(self.deal.id):
                    continue
                item = ranked.setdefault(str(chunk.id), {"chunk": chunk, "score": 0.0})
                item["score"] += 1 / (60 + rank)

        # Linked competitors get a scoped pass when larger document collections
        # dominate the global result set.
        if linked:
            for rank, chunk in enumerate(self._search(
                queries[0], limit=self.MAX_PROFILE_CHUNKS,
                deal_ids=list(linked), source_types=["document"],
            ), start=1):
                if chunk.source_type != "document" or str(chunk.deal_id) not in linked:
                    continue
                item = ranked.setdefault(str(chunk.id), {"chunk": chunk, "score": 0.0})
                item["score"] += 1 / (50 + rank)

        document_ids = [
            valid for item in ranked.values()
            if (valid := self._valid_uuid(item["chunk"].source_id))
        ]
        documents = {
            str(document.id): document
            for document in DealDocument.objects.filter(id__in=document_ids).select_related("deal")
        }
        source_info: dict[str, dict] = {}
        eligible = []
        for item in sorted(ranked.values(), key=lambda row: row["score"], reverse=True):
            chunk = item["chunk"]
            document = documents.get(str(chunk.source_id))
            if not document or str(document.deal_id) != str(chunk.deal_id):
                continue
            if not self._substantive(chunk, document.title):
                continue
            deal_id = str(document.deal_id)
            source_id = str(document.id)
            evidence_json = document.evidence_json if isinstance(document.evidence_json, dict) else {}
            source_metadata = evidence_json.get("source_metadata") or {}
            if not isinstance(source_metadata, dict):
                source_metadata = {}
            source_info[source_id] = {
                "company": document.deal.title,
                "deal_id": deal_id,
                "relationship": linked.get(deal_id, "semantic peer candidate"),
                "title": f"{document.deal.title}: {document.title}",
                "url": document.file_url or source_metadata.get("source_url") or "",
                "source_type": "document",
            }
            eligible.append(chunk)

        profile_source_ids = []
        for relation in VentureIntelligenceCompanyRelation.objects.filter(
            deal=self.deal, relation_type="competitor"
        ).select_related("company_profile"):
            company = relation.company_profile
            source_id = f"vi_{company.id}"
            profile_source_ids.append(source_id)
            source_info[source_id] = {
                "company": company.name,
                "deal_id": str(self.deal.id),
                "relationship": "linked competitor profile",
                "title": f"{company.name}: Venture Intelligence profile",
                "url": "", "source_type": "extracted_source",
            }
        if profile_source_ids:
            for chunk in self._search(
                queries[0], limit=self.MAX_PROFILE_CHUNKS,
                deal_ids=[str(self.deal.id)], source_ids=profile_source_ids,
                source_types=["extracted_source"],
            ):
                if (
                    str(chunk.source_id) in source_info
                    and str(chunk.deal_id) == str(self.deal.id)
                    and chunk.source_type == "extracted_source"
                    and self._substantive(chunk, source_info[str(chunk.source_id)]["title"])
                ):
                    eligible.append(chunk)

        return {
            "chunks": eligible,
            "source_info": source_info,
            "candidate_deal_count": len({
                str(chunk.deal_id) for chunk in eligible if chunk.source_type == "document"
            }),
        }
