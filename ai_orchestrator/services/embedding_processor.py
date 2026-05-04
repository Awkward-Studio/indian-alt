import logging
import re
from typing import List, Dict, Any, Optional
from django.db import connection
from django.db.models import Count
from django.utils import timezone
from langchain_text_splitters import RecursiveCharacterTextSplitter
from ..models import DealRetrievalProfile, DocumentChunk
from .runtime import AIRuntimeService
from deals.models import Deal, DealDocument, FolderAnalysisDocument
from deals.services.document_artifacts import DocumentArtifactService
from microsoft.models import Email
try:
    from .llm_providers import VLLMProviderService as BaseEmbeddingTransport
except ImportError:
    from .llm_providers import OllamaProviderService as BaseEmbeddingTransport

try:
    from .llm_providers import EmbeddingProviderService, RerankerProviderService
except ImportError:
    # Fall back to the shared provider transport if the dedicated providers are
    # unavailable in the deployed image.
    EmbeddingProviderService = BaseEmbeddingTransport

    class RerankerProviderService:
        def rerank(self, *, model: str, query: str, documents: list[str], timeout: int | None = None) -> list[dict[str, Any]]:
            return []

logger = logging.getLogger(__name__)

class EmbeddingService:
    """
    Service for chunking text, generating embeddings, and semantic retrieval.
    Supports pgvector for Postgres and keyword-fallback for SQLite.
    """

    RETRIEVAL_SOURCE_TYPES = ("document", "analysis_document", "deal_summary", "extracted_source")

    def __init__(self):
        self.provider = EmbeddingProviderService()
        self.reranker = RerankerProviderService()
        self.model_name = AIRuntimeService.get_embedding_model()
        self.reranker_model = AIRuntimeService.get_reranker_model()
        self.chunk_size = 1000
        self.chunk_overlap = 150
        self.is_sqlite = connection.vendor == 'sqlite'
        
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=self.chunk_size,
            chunk_overlap=self.chunk_overlap,
            length_function=len,
            separators=["\n\n", "\n", " ", ""]
        )

    def _get_embedding(self, text: str) -> List[float]:
        if self.is_sqlite: return [] # Skip embedding calls if on SQLite to save latency
        try:
            return self.provider.embed(model=self.model_name, text=text, timeout=30)
        except Exception as e:
            logger.error(f"Error generating embedding: {str(e)}")
            return []

    @staticmethod
    def _normalize_query_text(query: str) -> str:
        text = str(query or "").strip()
        if not text:
            return ""
        text = re.sub(r"\s+", " ", text)
        text = text.replace("|", " ")
        return text.strip()

    @staticmethod
    def _embedding_dimensions(embedding: Optional[List[float]]) -> Optional[int]:
        return len(embedding) if embedding else None

    def _retrievable_chunk_queryset(self):
        return (
            DocumentChunk.objects.exclude(content="")
            .exclude(embedding__isnull=True)
            .filter(source_type__in=self.RETRIEVAL_SOURCE_TYPES)
        )

    @staticmethod
    def _stringify(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, (int, float, bool)):
            return str(value)
        if isinstance(value, list):
            parts = [EmbeddingService._stringify(item) for item in value]
            return ", ".join(part for part in parts if part)
        if isinstance(value, dict):
            parts = []
            for key, item in value.items():
                rendered = EmbeddingService._stringify(item)
                if rendered:
                    parts.append(f"{key}: {rendered}")
            return "; ".join(parts)
        return str(value).strip()

    @staticmethod
    def _trim_text(value: Any, limit: int = 1200) -> str:
        text = EmbeddingService._stringify(value)
        if len(text) <= limit:
            return text
        return text[:limit].rstrip() + "..."

    def _profile_lines_from_metrics(self, metrics: Any) -> list[str]:
        if not metrics:
            return []
        lines: list[str] = []
        if isinstance(metrics, dict):
            for key, value in list(metrics.items())[:15]:
                rendered = self._trim_text(value, limit=180)
                if rendered:
                    lines.append(f"{key}: {rendered}")
            return lines
        if isinstance(metrics, list):
            for item in metrics[:15]:
                rendered = self._trim_text(item, limit=220)
                if rendered:
                    lines.append(rendered)
            return lines
        rendered = self._trim_text(metrics, limit=600)
        return [rendered] if rendered else []

    def build_deal_profile_text(self, deal: Deal) -> str:
        current_analysis = deal.current_analysis or {}
        canonical_snapshot = current_analysis.get("canonical_snapshot") if isinstance(current_analysis, dict) else {}
        deal_model_data = current_analysis.get("deal_model_data") if isinstance(current_analysis, dict) else {}
        if not isinstance(deal_model_data, dict):
            deal_model_data = {}

        snapshot_model_data = canonical_snapshot.get("deal_model_data") if isinstance(canonical_snapshot, dict) else {}
        if not isinstance(snapshot_model_data, dict):
            snapshot_model_data = {}

        merged_model_data = {**snapshot_model_data, **deal_model_data}
        metrics_blob = (
            merged_model_data.get("metrics")
            or current_analysis.get("metrics")
            or canonical_snapshot.get("metrics")
            or []
        ) if isinstance(current_analysis, dict) else []
        risks_blob = (
            current_analysis.get("risks")
            or merged_model_data.get("risks")
            or canonical_snapshot.get("risks")
            or []
        ) if isinstance(current_analysis, dict) else []

        documents = list(
            deal.documents.order_by("-created_at").values(
                "title",
                "document_type",
                "is_indexed",
                "normalized_text",
                "reasoning",
                "key_metrics_json",
            )[:30]
        )
        indexed_docs = [doc for doc in documents if doc.get("is_indexed")]
        doc_titles = [doc.get("title") for doc in documents if doc.get("title")]
        doc_type_counts = list(
            deal.documents.values("document_type").annotate(count=Count("id")).order_by("-count", "document_type")[:10]
        )

        chunk_samples = list(
            self._retrievable_chunk_queryset()
            .filter(deal=deal)
            .values("content", "metadata")
            .order_by("-created_at")[:24]
        )
        chunk_lines: list[str] = []
        seen_chunk_kinds: set[str] = set()
        for row in chunk_samples:
            metadata = row.get("metadata") or {}
            chunk_kind = str(metadata.get("chunk_kind") or "evidence")
            if chunk_kind in seen_chunk_kinds:
                continue
            preview = self._trim_text(row.get("content"), limit=320)
            if not preview:
                continue
            seen_chunk_kinds.add(chunk_kind)
            chunk_lines.append(f"{chunk_kind}: {preview}")
            if len(chunk_lines) >= 8:
                break

        document_text_samples: list[str] = []
        for doc in indexed_docs[:6]:
            title = doc.get("title") or "Untitled"
            normalized_text = self._trim_text(doc.get("normalized_text"), limit=300)
            reasoning = self._trim_text(doc.get("reasoning"), limit=220)
            metrics_lines = self._profile_lines_from_metrics(doc.get("key_metrics_json"))
            if normalized_text:
                document_text_samples.append(f"{title}: {normalized_text}")
            if reasoning:
                document_text_samples.append(f"{title} reasoning: {reasoning}")
            if metrics_lines:
                document_text_samples.append(f"{title} metrics: {' | '.join(metrics_lines[:4])}")
            if len(document_text_samples) >= 8:
                break

        sections = [
            "Deal Identity",
            f"Title: {deal.title or ''}",
            f"Industry: {deal.industry or ''}",
            f"Sector: {deal.sector or ''}",
            f"Geography: {', '.join(part for part in [deal.city, deal.state, deal.country] if part)}",
            f"Priority: {deal.priority or ''}",
            f"Current Phase: {deal.current_phase or ''}",
            f"Themes: {', '.join(deal.themes or []) if isinstance(deal.themes, list) else ''}",
            f"Female Led: {'yes' if deal.is_female_led else 'no'}",
            f"Management Meeting Complete: {'yes' if deal.management_meeting else 'no'}",
            "",
            "Commercial Summary",
            f"Funding Ask: {deal.funding_ask or ''}",
            f"Funding Ask For: {deal.funding_ask_for or ''}",
            f"Deal Summary: {self._trim_text(deal.deal_summary, limit=1500)}",
            f"Company Details: {self._trim_text(deal.company_details, limit=1200)}",
            f"Deal Details: {self._trim_text(deal.deal_details, limit=1200)}",
            f"Comments: {self._trim_text(deal.comments, limit=700)}",
            "",
            "Canonical Analysis",
            f"Analyst Report: {self._trim_text((canonical_snapshot or {}).get('analyst_report'), limit=1800)}",
            f"Business Description: {self._trim_text(merged_model_data.get('business_description') or merged_model_data.get('company_description'), limit=1000)}",
            f"Business Model: {self._trim_text(merged_model_data.get('business_model'), limit=800)}",
            f"Products And Services: {self._trim_text(merged_model_data.get('products') or merged_model_data.get('product_offering'), limit=800)}",
            f"Customers: {self._trim_text(merged_model_data.get('customers') or merged_model_data.get('customer_segment'), limit=800)}",
            f"Distribution: {self._trim_text(merged_model_data.get('distribution') or merged_model_data.get('channels'), limit=800)}",
            f"Geographic Presence: {self._trim_text(merged_model_data.get('geography') or merged_model_data.get('regions'), limit=600)}",
            f"Comparable Tags: {self._trim_text(merged_model_data.get('comparables') or merged_model_data.get('category_tags'), limit=600)}",
        ]

        metrics_lines = self._profile_lines_from_metrics(metrics_blob)
        if metrics_lines:
            sections.extend(["", "Key Metrics"] + metrics_lines[:15])

        rendered_risks = self._profile_lines_from_metrics(risks_blob)
        if rendered_risks:
            sections.extend(["", "Key Risks"] + rendered_risks[:10])

        if doc_type_counts or doc_titles:
            sections.append("")
            sections.append("Document Coverage")
            if doc_type_counts:
                sections.append(
                    "Document Types: " + ", ".join(
                        f"{row['document_type']} ({row['count']})" for row in doc_type_counts if row.get("document_type")
                    )
                )
            if doc_titles:
                sections.append("Document Titles: " + ", ".join(doc_titles[:20]))

        if document_text_samples:
            sections.extend(["", "Document Evidence"] + document_text_samples[:8])

        if chunk_lines:
            sections.extend(["", "Embedded Evidence"] + chunk_lines[:8])

        return "\n".join(part for part in sections if part).strip()

    def refresh_deal_profile(self, deal: Deal) -> bool:
        profile_text = self.build_deal_profile_text(deal)
        if len(profile_text.strip()) < 10:
            DealRetrievalProfile.objects.filter(deal=deal).delete()
            return False

        embedding = self._get_embedding(profile_text) if not self.is_sqlite else None
        profile, _ = DealRetrievalProfile.objects.update_or_create(
            deal=deal,
            defaults={
                "profile_text": profile_text,
                "embedding": embedding,
                "embedding_model": self.model_name,
                "embedding_dimensions": self._embedding_dimensions(embedding),
                "metadata": {
                    "title": deal.title,
                    "industry": deal.industry,
                    "sector": deal.sector,
                    "themes": deal.themes if isinstance(deal.themes, list) else [],
                },
                "indexed_at": timezone.now(),
            },
        )
        return bool(profile.profile_text)

    def chunk_and_embed(
        self,
        text: str,
        deal: Deal | None,
        source_type: str,
        source_id: str,
        metadata: Optional[Dict[str, Any]] = None,
        replace_existing: bool = False,
        audit_log=None,
    ) -> int:
        if not text or len(text.strip()) < 10:
            return 0
        if replace_existing:
            filters = {
                "source_type": source_type,
                "source_id": source_id,
            }
            if deal is not None:
                filters["deal"] = deal
            if audit_log is not None:
                filters["audit_log"] = audit_log
            DocumentChunk.objects.filter(**filters).delete()
        chunks = self.text_splitter.split_text(text)
        created_chunks = []
        for i, chunk_text in enumerate(chunks):
            embedding = self._get_embedding(chunk_text) if not self.is_sqlite else None
            chunk_metadata = metadata.copy() if metadata else {}
            chunk_metadata.update({"chunk_index": i, "total_chunks": len(chunks)})
            doc_chunk = DocumentChunk(
                deal=deal,
                audit_log=audit_log,
                source_type=source_type,
                source_id=source_id,
                content=chunk_text,
                embedding=embedding,
                embedding_model=self.model_name,
                embedding_dimensions=self._embedding_dimensions(embedding),
                indexed_at=timezone.now(),
                metadata=chunk_metadata,
            )
            created_chunks.append(doc_chunk)
        if created_chunks:
            DocumentChunk.objects.bulk_create(created_chunks)
            return len(created_chunks)
        return 0

    def _chunk_artifact_segment(
        self,
        *,
        text: str,
        base_metadata: Dict[str, Any],
        deal: Deal | None,
        source_type: str,
        source_id: str,
        audit_log=None,
    ) -> list[DocumentChunk]:
        if not text or len(text.strip()) < 10:
            return []
        split_texts = self.text_splitter.split_text(text)
        total_chunks = len(split_texts)
        created: list[DocumentChunk] = []
        for i, chunk_text in enumerate(split_texts):
            embedding = self._get_embedding(chunk_text) if not self.is_sqlite else None
            chunk_metadata = dict(base_metadata)
            chunk_metadata.update({"chunk_index": i, "total_chunks": total_chunks})
            created.append(
                DocumentChunk(
                    deal=deal,
                    audit_log=audit_log,
                    source_type=source_type,
                    source_id=source_id,
                    content=chunk_text,
                    embedding=embedding,
                    embedding_model=self.model_name,
                    embedding_dimensions=self._embedding_dimensions(embedding),
                    indexed_at=timezone.now(),
                    metadata=chunk_metadata,
                )
            )
        return created

    @staticmethod
    def _is_numeric_query(query: str) -> bool:
        upper = query.upper()
        numeric_markers = (
            "ARR", "MRR", "EBITDA", "MARGIN", "REVENUE", "DEBT", "CAPEX",
            "WORKING CAPITAL", "CUSTOMER CONCENTRATION", "VALUATION",
            "GMV", "CAC", "LTV", "BURN", "RUNWAY", "INR", "USD", "CR", "LAKH",
        )
        return any(marker in upper for marker in numeric_markers) or any(ch.isdigit() for ch in query)

    @classmethod
    def _chunk_kind_priority(cls, chunk: DocumentChunk, *, numeric_query: bool) -> int:
        metadata = chunk.metadata or {}
        chunk_kind = metadata.get("chunk_kind")
        if numeric_query:
            priorities = {
                "metric": 0,
                "table_summary": 1,
                "risk": 2,
                "claim": 3,
                "normalized_text": 4,
            }
        else:
            priorities = {
                "risk": 0,
                "claim": 1,
                "normalized_text": 2,
                "metric": 3,
                "table_summary": 4,
            }
        return priorities.get(chunk_kind, 5)

    def _rerank_chunks(self, chunks: List[DocumentChunk], query: str, limit: int) -> List[DocumentChunk]:
        if self.reranker_model and chunks:
            reranked = self._model_rerank_chunks(chunks, query=query, limit=limit)
            if reranked:
                return reranked

        numeric_query = self._is_numeric_query(query)

        def sort_key(chunk: DocumentChunk):
            distance = getattr(chunk, "distance", 0)
            return (self._chunk_kind_priority(chunk, numeric_query=numeric_query), distance)

        ordered = sorted(chunks, key=sort_key)
        deduped: list[DocumentChunk] = []
        seen: set[tuple[str, str, int]] = set()
        for chunk in ordered:
            metadata = chunk.metadata or {}
            identity = (
                str(chunk.source_id),
                str(metadata.get("chunk_kind")),
                int(metadata.get("chunk_index", 0) or 0),
            )
            if identity in seen:
                continue
            seen.add(identity)
            deduped.append(chunk)
            if len(deduped) >= limit:
                break
        return deduped

    def _candidate_fetch_limit(self, limit: int) -> int:
        safe_limit = max(int(limit or 0), 1)
        if self.reranker_model:
            return min(max(safe_limit * 2, 24), 96)
        return safe_limit * 6

    def _model_rerank_chunks(self, chunks: List[DocumentChunk], *, query: str, limit: int) -> List[DocumentChunk]:
        try:
            results = self.reranker.rerank(
                model=self.reranker_model,
                query=query,
                documents=[chunk.content for chunk in chunks],
            )
        except Exception as exc:
            logger.warning("Reranker failed, falling back to heuristic ordering: %s", exc)
            return []

        if not results:
            return []

        scored = []
        for item in results:
            index = item.get("index")
            if index is None or index < 0 or index >= len(chunks):
                continue
            chunk = chunks[index]
            setattr(chunk, "rerank_score", item.get("score"))
            scored.append((float(item.get("score") or 0.0), chunk))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        deduped: list[DocumentChunk] = []
        seen: set[tuple[str, str, int]] = set()
        for _, chunk in scored:
            metadata = chunk.metadata or {}
            identity = (
                str(chunk.source_id),
                str(metadata.get("chunk_kind")),
                int(metadata.get("chunk_index", 0) or 0),
            )
            if identity in seen:
                continue
            seen.add(identity)
            deduped.append(chunk)
            if len(deduped) >= limit:
                break
        return deduped

    def vectorize_email(self, email: Email) -> bool:
        if not email.extracted_text or not email.deal: return False
        created_count = self.chunk_and_embed(
            text=email.extracted_text,
            deal=email.deal,
            source_type='email',
            source_id=str(email.id),
            metadata={"subject": email.subject, "from": email.from_email},
            replace_existing=True,
        )
        if created_count:
            email.is_indexed = True
            email.save(update_fields=['is_indexed'])
        return bool(created_count)

    def vectorize_deal(self, deal: Deal) -> bool:
        if not deal.deal_summary: return False
        created_count = self.chunk_and_embed(
            text=deal.deal_summary,
            deal=deal,
            source_type='deal_summary',
            source_id=str(deal.id),
            metadata={"title": deal.title},
            replace_existing=True,
        )
        if created_count:
            self.refresh_deal_profile(deal)
            deal.is_indexed = True
            deal.save(update_fields=['is_indexed'])
        return bool(created_count)

    def vectorize_document(self, doc: DealDocument) -> bool:
        """Vectorizes a specific deal document artifact."""
        if not isinstance(doc, DealDocument):
            return False
        artifact = DocumentArtifactService.artifact_from_document(doc)
        if DocumentArtifactService.artifact_status(artifact) == DocumentArtifactService.STATUS_MISSING:
            return False

        DocumentChunk.objects.filter(
            deal=doc.deal,
            source_type='document',
            source_id=str(doc.id),
        ).delete()

        embedding_chunks = DocumentArtifactService.build_embedding_chunks(doc)
        created_chunks: list[DocumentChunk] = []
        for family_index, chunk_payload in enumerate(embedding_chunks):
            family_metadata = dict(chunk_payload.get("metadata") or {})
            family_metadata.update(
                {
                    "title": doc.title,
                    "type": doc.document_type,
                    "document_id": str(doc.id),
                    "family_index": family_index,
                }
            )
            created_chunks.extend(
                self._chunk_artifact_segment(
                    text=chunk_payload.get("text") or "",
                    base_metadata=family_metadata,
                    deal=doc.deal,
                    source_type='document',
                    source_id=str(doc.id),
                )
            )

        if created_chunks:
            DocumentChunk.objects.bulk_create(created_chunks)
        created_count = len(created_chunks)
        if created_count:
            doc.is_indexed = True
            doc.chunking_status = "chunked"
            doc.last_chunked_at = timezone.now()
            doc.save(update_fields=['is_indexed', 'chunking_status', 'last_chunked_at'])
            self.refresh_deal_profile(doc.deal)
        else:
            doc.is_indexed = False
            doc.chunking_status = "failed"
            doc.save(update_fields=['is_indexed', 'chunking_status'])
        return bool(created_count)

    def vectorize_analysis_document(self, doc: FolderAnalysisDocument) -> bool:
        """Vectorizes a persisted pre-deal analysis document against its audit log."""
        if not isinstance(doc, FolderAnalysisDocument):
            return False
        artifact = DocumentArtifactService.artifact_from_analysis_document(doc)
        if DocumentArtifactService.artifact_status(artifact) == DocumentArtifactService.STATUS_MISSING:
            return False

        DocumentChunk.objects.filter(
            audit_log=doc.audit_log,
            source_type='analysis_document',
            source_id=str(doc.id),
        ).delete()

        embedding_chunks = DocumentArtifactService.build_embedding_chunks(doc)
        created_chunks: list[DocumentChunk] = []
        for family_index, chunk_payload in enumerate(embedding_chunks):
            family_metadata = dict(chunk_payload.get("metadata") or {})
            family_metadata.update(
                {
                    "title": doc.file_name,
                    "type": doc.document_type,
                    "analysis_document_id": str(doc.id),
                    "source_file_id": doc.source_file_id,
                    "audit_log_id": str(doc.audit_log_id),
                    "family_index": family_index,
                }
            )
            created_chunks.extend(
                self._chunk_artifact_segment(
                    text=chunk_payload.get("text") or "",
                    base_metadata=family_metadata,
                    deal=None,
                    audit_log=doc.audit_log,
                    source_type='analysis_document',
                    source_id=str(doc.id),
                )
            )

        if created_chunks:
            DocumentChunk.objects.bulk_create(created_chunks)
        created_count = len(created_chunks)
        if created_count:
            doc.is_indexed = True
            doc.chunk_count = created_count
            doc.chunking_status = "chunked"
            doc.last_chunked_at = timezone.now()
            doc.save(update_fields=['is_indexed', 'chunk_count', 'chunking_status', 'last_chunked_at'])
        else:
            doc.is_indexed = False
            doc.chunk_count = 0
            doc.chunking_status = "failed"
            doc.save(update_fields=['is_indexed', 'chunk_count', 'chunking_status'])
        return bool(created_count)

    def search_similar_chunks(self, query: str, deal: Deal, limit: int = 5) -> List[DocumentChunk]:
        """Hybrid Search: Vector for Postgres, Ranked Keyword for SQLite."""
        normalized_query = self._normalize_query_text(query)
        if self.is_sqlite:
            from django.db.models import Q
            words = [w.lower() for w in normalized_query.split() if len(w) >= 3]
            if not words: return []

            queryset = self._retrievable_chunk_queryset().filter(deal=deal)
            important_terms = [w for w in words if any(x in w.upper() for x in ['CM', 'ARR', 'INR', 'CR'])]
            q_obj = Q()
            if important_terms:
                for term in important_terms:
                    q_obj |= Q(content__icontains=term)
            else:
                for word in words[:5]:
                    q_obj |= Q(content__icontains=word)
            candidates = list(queryset.filter(q_obj)[: self._candidate_fetch_limit(limit)])
            return self._rerank_chunks(candidates, normalized_query, limit)
        
        from pgvector.django import CosineDistance
        query_embedding = self._get_embedding(normalized_query)
        if not query_embedding: return []
        candidates = list(
            self._retrievable_chunk_queryset()
            .filter(deal=deal)
            .annotate(distance=CosineDistance('embedding', query_embedding))
            .order_by('distance')[: self._candidate_fetch_limit(limit)]
        )
        return self._rerank_chunks(candidates, normalized_query, limit)

    def search_global_chunks(self, query: str, limit: int = 10, deal_ids: Optional[List[str]] = None, source_ids: Optional[List[str]] = None) -> List[DocumentChunk]:
        """Global search across all deals with term priority for SQLite."""
        normalized_query = self._normalize_query_text(query)
        if self.is_sqlite:
            from django.db.models import Q
            words = [w.lower() for w in normalized_query.split() if len(w) >= 3]
            if not words: return []
            
            important_terms = [w for w in words if any(x in w.upper() for x in ['CM', 'ARR', 'INR', 'CR'])]
            q_obj = Q()
            if important_terms:
                for term in important_terms: q_obj |= Q(content__icontains=term)
            else:
                for word in words[:5]: q_obj |= Q(content__icontains=word)
            queryset = self._retrievable_chunk_queryset().filter(q_obj)
            if deal_ids:
                queryset = queryset.filter(deal_id__in=deal_ids)
            if source_ids:
                queryset = queryset.filter(source_id__in=source_ids)
            candidates = list(queryset[: self._candidate_fetch_limit(limit)])
            return self._rerank_chunks(candidates, normalized_query, limit)

        from pgvector.django import CosineDistance
        query_embedding = self._get_embedding(normalized_query)
        if not query_embedding: return []
        queryset = self._retrievable_chunk_queryset()
        if deal_ids:
            queryset = queryset.filter(deal_id__in=deal_ids)
        if source_ids:
            queryset = queryset.filter(source_id__in=source_ids)
        candidates = list(
            queryset.annotate(distance=CosineDistance('embedding', query_embedding))
            .order_by('distance')[: self._candidate_fetch_limit(limit)]
        )
        return self._rerank_chunks(candidates, normalized_query, limit)

    def search_deal_profiles(self, query: str, *, limit: int = 10, filters: Optional[Dict[str, Any]] = None) -> List[Deal]:
        filters = filters or {}
        normalized_query = self._normalize_query_text(query)
        queryset = Deal.objects.all()
        if "is_female_led" in filters:
            queryset = queryset.filter(is_female_led=filters["is_female_led"])
        if "management_meeting" in filters:
            queryset = queryset.filter(management_meeting=filters["management_meeting"])
        for field in ["title", "industry", "sector", "city", "priority", "current_phase"]:
            value = filters.get(field)
            if value:
                queryset = queryset.filter(**{f"{field}__icontains": str(value)})

        if self.is_sqlite:
            words = [word for word in normalized_query.split() if len(word) >= 3]
            if not words:
                return list(queryset.order_by("-created_at")[:limit])
            q = None
            from django.db.models import Q
            for word in words[:6]:
                clause = Q(retrieval_profile__profile_text__icontains=word) | Q(title__icontains=word) | Q(deal_summary__icontains=word)
                q = clause if q is None else q | clause
            if q is None:
                return list(queryset.order_by("-created_at")[:limit])
            return list(queryset.filter(q).distinct()[:limit])

        from pgvector.django import CosineDistance
        query_embedding = self._get_embedding(normalized_query)
        if not query_embedding:
            return list(queryset.order_by("-created_at")[:limit])

        profiles = list(
            DealRetrievalProfile.objects.select_related("deal")
            .filter(deal__in=queryset)
            .annotate(distance=CosineDistance("embedding", query_embedding))
            .order_by("distance")[: limit * 2]
        )
        ordered: list[Deal] = []
        seen: set[str] = set()
        for profile in profiles:
            deal_id = str(profile.deal_id)
            if deal_id in seen:
                continue
            setattr(profile.deal, "retrieval_distance", getattr(profile, "distance", None))
            ordered.append(profile.deal)
            seen.add(deal_id)
            if len(ordered) >= limit:
                break
        if ordered:
            return ordered
        return list(queryset.order_by("-created_at")[:limit])
