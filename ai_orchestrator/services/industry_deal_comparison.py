"""Retrieve source-backed internal peers for the IC Industry Overview."""

from __future__ import annotations

import logging
import re
import time
from collections import OrderedDict
from uuid import UUID

from django.db import connection
from django.db.models import Q

from deals.models import Deal, DealDocument, DealRelationshipContext, VentureIntelligenceCompanyRelation

logger = logging.getLogger(__name__)


class IndustryDealComparisonService:
    """Rank deal-document passages globally, without an industry gate."""

    MAX_RANKED_CHUNKS_PER_QUERY = 160
    MAX_PROFILE_CHUNKS = 48
    MAX_RERANK_CHUNKS = 96
    RERANK_BATCH_SIZE = 32
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
        started = time.monotonic()
        prior_ef_search = None
        try:
            # Gather candidates first. Rerank the merged pool once, in VM-sized
            # batches, instead of reranking every global search independently.
            with connection.cursor() as cursor:
                # Loading a vector operator registers pgvector's HNSW GUCs on
                # this connection before current_setting() reads them.
                cursor.execute("SELECT '[1,2]'::vector <=> '[1,2]'::vector")
                cursor.execute("SELECT current_setting('hnsw.ef_search')")
                prior_ef_search = cursor.fetchone()[0]
                cursor.execute(
                    "SELECT set_config('hnsw.ef_search', %s, false)",
                    [str(max(200, int(limit)))],
                )
            chunks = self.embedding_service.search_global_chunks(query, limit=limit, rerank=False, **filters)
            logger.info(
                "Industry peer search retrieved %s chunks in %.1fs (deal=%s, scoped=%s)",
                len(chunks), time.monotonic() - started, self.deal.id, bool(filters.get("deal_ids")),
            )
            return chunks
        except Exception as exc:
            logger.warning("Industry peer search failed after %.1fs: %s", time.monotonic() - started, exc)
            return []
        finally:
            if prior_ef_search is not None:
                try:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT set_config('hnsw.ef_search', %s, false)",
                            [str(prior_ef_search)],
                        )
                except Exception as exc:
                    logger.warning("Could not restore hnsw.ef_search after Industry search: %s", exc)

    def _rerank(self, items: list[dict], query: str) -> list[dict]:
        model = getattr(self.embedding_service, "reranker_model", "")
        reranker = getattr(self.embedding_service, "reranker", None)
        if not isinstance(model, str) or not model or reranker is None:
            return items

        shortlist = items[:self.MAX_RERANK_CHUNKS]
        scores: dict[str, float] = {}
        started = time.monotonic()
        try:
            for start in range(0, len(shortlist), self.RERANK_BATCH_SIZE):
                batch = shortlist[start:start + self.RERANK_BATCH_SIZE]
                results = reranker.rerank(
                    model=model,
                    query=query[:700],
                    documents=[str(item["chunk"].content or "")[:2_500] for item in batch],
                )
                for result in results or []:
                    index = int(result["index"])
                    if 0 <= index < len(batch):
                        scores[str(batch[index]["chunk"].id)] = float(result["score"])
        except Exception as exc:
            logger.warning("Industry peer rerank failed after %.1fs: %s", time.monotonic() - started, exc)
            return items
        if not scores:
            return items
        logger.info(
            "Industry peer reranked %s chunks in %.1fs (deal=%s)",
            len(scores), time.monotonic() - started, self.deal.id,
        )
        # Keep the source-ranked tail available for broad company coverage.
        shortlist.sort(
            key=lambda item: (scores.get(str(item["chunk"].id), float("-inf")), item["score"]),
            reverse=True,
        )
        return shortlist + items[self.MAX_RERANK_CHUNKS:]

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
            f"{profile}. Comparable-company source evidence for operating scale and financial "
            "performance: revenue and sales, gross profit and margins, EBITDA and PAT, "
            "customers, pricing, capacity and retention. Prefer primary source tables and "
            "preserve exact values, currency, units, periods, actuals versus forecasts, "
            "and page or source locations. Include relevant evidence even when the company "
            "is only a semantic peer candidate.",
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
        ordered = self._rerank(
            sorted(ranked.values(), key=lambda row: row["score"], reverse=True),
            (
                "Find comparable companies with overlapping products, buyers, business model "
                "and geography. Prioritize source-backed operating and financial evidence. "
                f"Target company: {profile[:500]}"
            ),
        )
        for item in ordered:
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
