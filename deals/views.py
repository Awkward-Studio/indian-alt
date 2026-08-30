import logging
from collections import defaultdict
from decimal import Decimal, InvalidOperation

import django_filters.rest_framework as django_filters
from rest_framework import viewsets, filters, status
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from django.db import transaction
from django.db.models import CharField, Count, Exists, F, OuterRef, Q, Value
from django.db.models.functions import Coalesce, Trim
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from drf_spectacular.utils import extend_schema, extend_schema_view
from core.mixins import ErrorHandlingMixin
from .models import (
    Deal, DealAnalysis, DealContradiction, DealDocument, DealPhase, DealPhaseLog,
    DealPassReasonRemediationAudit,
    DealReceiptDateAudit, DealReceiptDateSuggestion,
    FundClassificationSourceType, FundClassificationState,
    DealRelationshipContext, SectorResearchAcquisition, SectorResearchDiscoveryRun,
    SectorResearchRecommendation, SectorResearchSourceRule,
    VentureIntelligenceCompanyRelation,
)
from .serializers import (
    DealSerializer, DealListSerializer, DealDetailSerializer,
    DealContradictionSerializer, DealDocumentSerializer, DealPhaseLogSerializer,
    DealHeavyFieldsSerializer, SectorResearchAcquisitionSerializer, SectorResearchDiscoveryRunSerializer,
    SectorResearchRecommendationSerializer, SectorResearchSourceRuleSerializer,
)
from .services.deal_creation import DealCreationService
from .services.document_artifacts import DocumentArtifactService
from .services.deal_flow import DealFlowService, DealFlowValidationError
from .services.folder_analysis import FolderAnalysisService
from .services.receipt_date_evidence import ReceiptDateEvidenceService
from ai_orchestrator.models import AIAuditLog, DocumentChunk
from ai_orchestrator.services.runtime import AIRuntimeService

logger = logging.getLogger(__name__)


class SectorResearchSourceRuleViewSet(viewsets.ModelViewSet):
    serializer_class = SectorResearchSourceRuleSerializer
    queryset = SectorResearchSourceRule.objects.all()
    permission_classes = [IsAuthenticated]
    pagination_class = None

    def _is_admin(self):
        profile = getattr(self.request.user, "profile", None)
        return bool(self.request.user.is_staff or getattr(profile, "is_admin", False))

    def create(self, request, *args, **kwargs):
        if not self._is_admin():
            return Response({"detail": "Admin access is required."}, status=status.HTTP_403_FORBIDDEN)
        return super().create(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        if not self._is_admin():
            return Response({"detail": "Admin access is required."}, status=status.HTTP_403_FORBIDDEN)
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        if not self._is_admin():
            return Response({"detail": "Admin access is required."}, status=status.HTTP_403_FORBIDDEN)
        return super().destroy(request, *args, **kwargs)


class DealOrderingFilter(filters.OrderingFilter):
    """Keep undated deals after dated deals for either receipt-date direction."""

    def filter_queryset(self, request, queryset, view):
        ordering = self.get_ordering(request, queryset, view)
        if not ordering:
            return queryset

        expressions = []
        for field in ordering:
            if field.lstrip('-') == 'received_at':
                expression = F('received_at')
                expressions.append(
                    expression.desc(nulls_last=True)
                    if field.startswith('-')
                    else expression.asc(nulls_last=True)
                )
            else:
                expressions.append(field)
        return queryset.order_by(*expressions)


def serialize_vi_cin_candidates(resolution):
    candidates = resolution.get("cin_candidates") or []
    if not isinstance(candidates, list):
        return []

    serialized = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        serialized.append({
            "cin": candidate.get("cin"),
            "entity_name": candidate.get("entity_name"),
            "confidence": candidate.get("confidence"),
            "source": candidate.get("source"),
            "rationale": candidate.get("rationale"),
            "used": candidate.get("cin") == resolution.get("cin"),
        })
    return serialized


class DealPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


FUND_FILTER_ALIASES = {
    'FUND1': ['FUND1', 'Fund I', 'Fund 1'],
    'FUND2': ['FUND2', 'Fund II', 'Fund 2'],
    'FUND3': ['FUND3', 'Fund III', 'Fund 3'],
}


def filter_by_fund_alias(queryset, fund):
    aliases = FUND_FILTER_ALIASES.get(fund, [])
    if not aliases:
        return queryset.none()
    query = Q()
    for alias in aliases:
        query |= Q(fund__iexact=alias)
    return queryset.filter(query)


class DealFilterSet(django_filters.FilterSet):
    deal_group = django_filters.ChoiceFilter(
        choices=[
            ('active', 'In process'),
            ('passed', 'Passed'),
            ('portfolio', 'Portfolio'),
        ],
        method='filter_deal_group',
    )
    fund = django_filters.ChoiceFilter(
        choices=[
            ('FUND1', 'Fund I'),
            ('FUND2', 'Fund II'),
            ('FUND3', 'Fund III'),
        ],
        method='filter_fund',
    )
    sector = django_filters.CharFilter(lookup_expr='icontains')
    city = django_filters.CharFilter(lookup_expr='icontains')
    bank_name = django_filters.CharFilter(field_name='bank__name', lookup_expr='icontains')
    banker_name = django_filters.CharFilter(field_name='primary_contact__name', lookup_expr='icontains')
    fund_classification_state = django_filters.ChoiceFilter(
        choices=FundClassificationState.choices
    )
    has_analysis = django_filters.BooleanFilter(field_name='has_analysis')
    has_vi_data = django_filters.BooleanFilter(field_name='has_vi_data')
    has_competitors = django_filters.BooleanFilter(
        method='filter_has_competitors'
    )

    def filter_deal_group(self, queryset, name, value):
        terminal_statuses = ['Passed', 'Invested', 'Portfolio']
        if value == 'portfolio':
            return queryset.filter(current_phase__in=['Invested', 'Portfolio'])
        if value == 'passed':
            return queryset.filter(current_phase='Passed')
        if value == 'active':
            return queryset.exclude(current_phase__in=terminal_statuses)
        return queryset

    def filter_fund(self, queryset, name, value):
        return filter_by_fund_alias(queryset, value)

    def filter_has_competitors(self, queryset, name, value):
        competitor_data = (
            Q(has_vi_competitors=True)
            | Q(has_relationship_competitors=True)
            | (
                Q(competitor_candidates__isnull=False)
                & ~Q(competitor_candidates=[])
            )
        )
        return queryset.filter(competitor_data) if value else queryset.exclude(
            competitor_data
        )

    class Meta:
        model = Deal
        fields = [
            'bank', 'priority', 'deal_status', 'deal_group', 'fund', 'is_female_led',
            'management_meeting', 'business_proposal_stage', 'ic_stage',
            'current_phase', 'sector', 'city', 'primary_contact',
            'bank_name', 'banker_name',
            'fund_classification_state',
            'has_analysis', 'has_vi_data', 'has_competitors',
        ]


@extend_schema_view(
    list=extend_schema(
        summary="List all deal documents",
        description="Retrieve a list of all documents associated with deals.",
        tags=["Deal Documents"],
    ),
)
class DealDocumentViewSet(ErrorHandlingMixin, viewsets.ModelViewSet):
    queryset = DealDocument.objects.all()
    serializer_class = DealDocumentSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['title', 'normalized_text', 'reasoning']
    filterset_fields = ['deal', 'document_type', 'is_indexed']
    ordering_fields = ['created_at', 'title', 'document_type']
    ordering = ['-created_at']

    @transaction.atomic
    def perform_create(self, serializer):
        if hasattr(self.request.user, 'profile'):
            serializer.save(uploaded_by=self.request.user.profile)
        else:
            serializer.save()

    @action(detail=False, methods=['post'])
    def search(self, request):
        """
        Semantic search/RAG across all indexed documents.
        """
        query = request.data.get('query')
        deal_id = request.data.get('deal_id') # Optional filter
        
        if not query:
            return Response({"error": "query is required"}, status=400)
            
        try:
            from ai_orchestrator.services.ai_processor import AIProcessorService
            from ai_orchestrator.services.embedding_processor import EmbeddingService
            
            docs = DealDocument.objects.filter(is_indexed=True)
            if deal_id:
                docs = docs.filter(deal_id=deal_id)
                
            if not docs.exists():
                return Response({"response": "No indexed documents found to search through."}, status=200)

            embed_service = EmbeddingService()
            if deal_id:
                chunks = embed_service.search_similar_chunks(query, docs.first().deal, limit=10)
            else:
                chunks = embed_service.search_global_chunks(query, limit=10)
                allowed_ids = {str(doc.id) for doc in docs}
                chunks = [
                    chunk for chunk in chunks
                    if chunk.source_type != 'document' or chunk.source_id in allowed_ids
                ]

            doc_ids = {
                chunk.source_id for chunk in chunks
                if chunk.source_type == 'document' and chunk.source_id
            }
            matched_docs = {
                str(doc.id): doc
                for doc in docs.filter(id__in=doc_ids).select_related('deal')
            }

            evidence_blocks = []
            for doc in matched_docs.values():
                artifact = DocumentArtifactService.artifact_from_document(doc)
                evidence_blocks.append(
                    {
                        "deal_title": doc.deal.title,
                        "artifact_status": DocumentArtifactService.artifact_status(artifact),
                        "document_evidence": artifact,
                    }
                )

            chunk_blocks = []
            for chunk in chunks:
                title = (chunk.metadata or {}).get('title') or 'Source'
                chunk_blocks.append(
                    {
                        "source_id": chunk.source_id,
                        "source_type": chunk.source_type,
                        "title": title,
                        "chunk_kind": (chunk.metadata or {}).get("chunk_kind") or "text",
                        "citation_label": (chunk.metadata or {}).get("citation_label") or title,
                        "content": chunk.content[:1600],
                        "metadata": chunk.metadata or {},
                    }
                )

            context = (
                "[DOCUMENT EVIDENCE]\n"
                f"{evidence_blocks}\n\n"
                "[MATCHED CHUNKS]\n"
                f"{chunk_blocks}"
            )

            ai_service = AIProcessorService()
            from ai_orchestrator.services.pipeline_registry import PipelineRegistryService
            stage = PipelineRegistryService.resolve_stage(
                "analysis_support", "global_document_search"
            )
            
            result = ai_service.process_content(
                content="",
                skill_name=None,
                source_type="global_search",
                metadata={
                    "pipeline_key": stage.pipeline.key,
                    "stage_key": stage.stage.key,
                    "query": query,
                    "context": context,
                },
            )
            
            return Response({
                "query": query,
                "response": result.get('_raw_response', 'No answer generated.'),
                "thinking": result.get('thinking', '')
            })
        except Exception as e:
            return Response({"error": str(e)}, status=500)


@extend_schema_view(
    list=extend_schema(
        summary="List all deals",
        description="Retrieve a list of all deals with optional filtering and search.",
        tags=["Deals"],
    ),
    create=extend_schema(
        summary="Create a new deal",
        description="Create a new deal record.",
        tags=["Deals"],
    ),
    retrieve=extend_schema(
        summary="Retrieve a deal",
        description="Get detailed information about a specific deal.",
        tags=["Deals"],
    ),
    update=extend_schema(
        summary="Update a deal",
        description="Update all fields of a deal record.",
        tags=["Deals"],
    ),
    partial_update=extend_schema(
        summary="Partially update a deal",
        description="Update specific fields of a deal record.",
        tags=["Deals"],
    ),
    destroy=extend_schema(
        summary="Delete a deal",
        description="Delete a deal record.",
        tags=["Deals"],
    ),
)
class DealViewSet(ErrorHandlingMixin, viewsets.ModelViewSet):
    # Use select_related to avoid N+1 queries on foreign keys
    queryset = Deal.objects.select_related(
        'bank', 'primary_contact', 'request'
    ).prefetch_related(
        'responsibility', 'additional_contacts', 'field_provenance'
    ).annotate(
        has_analysis=Exists(
            DealAnalysis.objects.filter(deal_id=OuterRef('pk'))
        ),
        has_vi_data=Exists(
            VentureIntelligenceCompanyRelation.objects.filter(
                deal_id=OuterRef('pk')
            )
        ),
        has_vi_competitors=Exists(
            VentureIntelligenceCompanyRelation.objects.filter(
                deal_id=OuterRef('pk'),
                relation_type='competitor',
            )
        ),
        has_relationship_competitors=Exists(
            DealRelationshipContext.objects.filter(
                deal_id=OuterRef('pk'),
                relationship_type='competitor',
            )
        ),
        has_receipt_date_suggestions=Exists(
            DealReceiptDateSuggestion.objects.filter(
                deal_id=OuterRef('pk'),
                status__in=['PENDING', 'CONFLICT'],
            )
        ),
        has_receipt_date_conflicts=Exists(
            DealReceiptDateSuggestion.objects.filter(
                deal_id=OuterRef('pk'),
                status='CONFLICT',
            )
        ),
        has_receipt_date_rejections=Exists(
            DealReceiptDateSuggestion.objects.filter(
                deal_id=OuterRef('pk'),
                status='REJECTED',
            )
        ),
    )
    permission_classes = [IsAuthenticated]
    pagination_class = DealPagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, DealOrderingFilter]
    filterset_class = DealFilterSet
    search_fields = [
        'title', 'deal_summary', 'industry', 'sector', 'city', 'state',
        'country', 'fund', 'priority', 'deal_status', 'current_phase',
        'funding_ask_for', 'bank__name',
        'legacy_investment_bank', 'primary_contact__name'
    ]
    ordering_fields = [
        'received_at', 'created_at', 'title', 'priority', 'deal_status',
        'sector', 'industry', 'fund', 'current_phase', 'city', 'funding_ask',
        'is_female_led', 'has_analysis', 'has_vi_data', 'bank__name',
        'primary_contact__name',
    ]
    ordering = ['-received_at', '-created_at']
    @staticmethod
    def _parse_funding_ask(value):
        if value in (None, ''):
            return 0.0

        cleaned_value = str(value).strip().replace(',', '')
        try:
            return float(Decimal(cleaned_value))
        except (InvalidOperation, ValueError, TypeError):
            return 0.0

    @staticmethod
    def _isoformat(value):
        return value.isoformat() if value else None

    @staticmethod
    def _truncate_text(value, limit=500):
        compact = " ".join((value or "").split())
        if len(compact) <= limit:
            return compact
        return f"{compact[:limit].rstrip()}..."

    @staticmethod
    def _chunk_kind(chunk):
        metadata = chunk.metadata if isinstance(chunk.metadata, dict) else {}
        return metadata.get("chunk_kind") or metadata.get("kind") or "text"

    @staticmethod
    def _chunk_source_title(chunk, documents_by_id):
        metadata = chunk.metadata if isinstance(chunk.metadata, dict) else {}
        if chunk.source_type == "document" and chunk.source_id:
            document = documents_by_id.get(str(chunk.source_id))
            if document:
                return document.title
        return (
            metadata.get("title")
            or metadata.get("filename")
            or metadata.get("document_name")
            or metadata.get("citation_label")
            or chunk.source_type.replace("_", " ").title()
        )

    def _serialize_chunk_inventory_item(self, chunk, documents_by_id):
        return {
            "id": str(chunk.id),
            "source_type": chunk.source_type,
            "source_id": chunk.source_id,
            "source_title": self._chunk_source_title(chunk, documents_by_id),
            "chunk_kind": self._chunk_kind(chunk),
            "content_preview": self._truncate_text(chunk.content, 700),
            "content_length": len(chunk.content or ""),
            "metadata": chunk.metadata or {},
            "embedding": {
                "is_embedded": chunk.embedding is not None,
                "model": chunk.embedding_model,
                "dimensions": chunk.embedding_dimensions,
                "indexed_at": self._isoformat(chunk.indexed_at),
            },
            "created_at": self._isoformat(chunk.created_at),
        }

    @action(detail=True, methods=['get'])
    def chunks(self, request, pk=None):
        """
        Paginated inventory of chunks and embeddings available to local deal chat.
        """
        deal = self.get_object()
        documents_by_id = {
            str(document.id): document
            for document in deal.documents.all()
        }

        queryset = DocumentChunk.objects.filter(deal=deal).order_by('-indexed_at', '-created_at')
        source_type = request.query_params.get("source_type")
        source_id = request.query_params.get("source_id")
        chunk_kind = request.query_params.get("chunk_kind")
        embedded = request.query_params.get("embedded")

        if source_type:
            queryset = queryset.filter(source_type=source_type)
        if source_id:
            queryset = queryset.filter(source_id=source_id)
        if chunk_kind:
            queryset = queryset.filter(metadata__chunk_kind=chunk_kind)
        if embedded in ("true", "1", "yes"):
            queryset = queryset.filter(embedding__isnull=False)
        elif embedded in ("false", "0", "no"):
            queryset = queryset.filter(embedding__isnull=True)

        paginator = DealPagination()
        page = paginator.paginate_queryset(queryset, request)
        items = [
            self._serialize_chunk_inventory_item(chunk, documents_by_id)
            for chunk in (page if page is not None else queryset)
        ]
        return paginator.get_paginated_response(items)

    @action(detail=True, methods=['get'])
    def knowledge_graph(self, request, pk=None):
        """
        Evidence map for the deal: documents, chunks, analyses, generated outputs,
        VI profiles, and saved relationship contexts.
        """
        deal = self.get_object()
        documents = list(deal.documents.all().order_by('-created_at'))
        documents_by_id = {str(document.id): document for document in documents}
        chunks = list(DocumentChunk.objects.filter(deal=deal).order_by('-indexed_at', '-created_at'))
        graph_chunk_node_limit = min(int(request.query_params.get("chunk_node_limit", 150)), 300)

        nodes = []
        edges = []
        node_ids = set()
        edge_ids = set()

        def add_node(node_id, node_type, label, status=None, metadata=None):
            if node_id in node_ids:
                return
            node_ids.add(node_id)
            nodes.append({
                "id": node_id,
                "type": node_type,
                "label": label,
                "status": status,
                "metadata": metadata or {},
            })

        def add_edge(source, target, relationship, metadata=None):
            if source not in node_ids or target not in node_ids:
                return
            edge_id = f"{source}->{target}:{relationship}"
            if edge_id in edge_ids:
                return
            edge_ids.add(edge_id)
            edges.append({
                "id": edge_id,
                "source": source,
                "target": target,
                "relationship": relationship,
                "metadata": metadata or {},
            })

        deal_node_id = f"deal:{deal.id}"
        add_node(deal_node_id, "deal", deal.title, deal.current_phase, {
            "priority": deal.priority,
            "deal_status": deal.deal_status,
            "sector": deal.sector,
            "industry": deal.industry,
            "fund": deal.fund,
        })

        for document in documents:
            artifact_status = None
            try:
                artifact_status = DocumentArtifactService.artifact_status(
                    DocumentArtifactService.artifact_from_document(document)
                )
            except Exception:
                artifact_status = None
            status_value = "indexed" if document.is_indexed else document.chunking_status or "not_indexed"
            document_node_id = f"document:{document.id}"
            add_node(document_node_id, "document", document.title, status_value, {
                "document_type": document.document_type,
                "is_indexed": document.is_indexed,
                "is_ai_analyzed": document.is_ai_analyzed,
                "initial_analysis_status": document.initial_analysis_status,
                "transcription_status": document.transcription_status,
                "chunking_status": document.chunking_status,
                "artifact_status": artifact_status,
                "onedrive_id": document.onedrive_id,
                "created_at": self._isoformat(document.created_at),
            })
            add_edge(deal_node_id, document_node_id, "has_document")

        chunks_by_source = defaultdict(list)
        for chunk in chunks:
            chunks_by_source[(chunk.source_type, chunk.source_id or "none")].append(chunk)

        chunk_node_ids_by_chunk_id = {}
        rendered_chunk_count = 0

        for (source_type, source_id), source_chunks in chunks_by_source.items():
            first_chunk = source_chunks[0]
            source_title = self._chunk_source_title(first_chunk, documents_by_id)
            embedded_count = sum(1 for chunk in source_chunks if chunk.embedding is not None)
            group_status = (
                "embedded" if embedded_count == len(source_chunks)
                else "partial" if embedded_count > 0
                else "not_embedded"
            )
            group_id = f"chunk_group:{source_type}:{source_id}"
            add_node(group_id, "chunk_group", source_title, group_status, {
                "source_type": source_type,
                "source_id": None if source_id == "none" else source_id,
                "chunk_count": len(source_chunks),
                "embedded_chunk_count": embedded_count,
                "chunk_kinds": sorted({self._chunk_kind(chunk) for chunk in source_chunks}),
            })

            parent_id = deal_node_id
            if source_type == "document" and str(source_id) in documents_by_id:
                parent_id = f"document:{source_id}"
            add_edge(parent_id, group_id, "has_chunks")

            for chunk in source_chunks:
                if rendered_chunk_count >= graph_chunk_node_limit:
                    continue
                chunk_id = f"chunk:{chunk.id}"
                chunk_node_ids_by_chunk_id[str(chunk.id)] = chunk_id
                rendered_chunk_count += 1
                add_node(chunk_id, "chunk", f"{self._chunk_kind(chunk).replace('_', ' ').title()} Chunk", "embedded" if chunk.embedding is not None else "not_embedded", {
                    "source_type": chunk.source_type,
                    "source_id": chunk.source_id,
                    "source_title": source_title,
                    "chunk_kind": self._chunk_kind(chunk),
                    "content_preview": self._truncate_text(chunk.content, 500),
                    "content_length": len(chunk.content or ""),
                    "embedding_model": chunk.embedding_model,
                    "embedding_dimensions": chunk.embedding_dimensions,
                    "indexed_at": self._isoformat(chunk.indexed_at),
                    "created_at": self._isoformat(chunk.created_at),
                })
                add_edge(group_id, chunk_id, "contains_chunk")

        analyses = list(deal.analyses.all().order_by('-version', '-created_at'))
        for analysis in analyses:
            analysis_node_id = f"analysis:{analysis.id}"
            analysis_json = analysis.analysis_json if isinstance(analysis.analysis_json, dict) else {}
            metadata = analysis_json.get("metadata") if isinstance(analysis_json.get("metadata"), dict) else {}
            add_node(analysis_node_id, "analysis", f"Analysis v{analysis.version}", analysis.analysis_kind, {
                "version": analysis.version,
                "analysis_kind": analysis.analysis_kind,
                "documents_analyzed": metadata.get("analysis_input_files") or [],
                "failed_files": metadata.get("failed_files") or [],
                "ambiguity_count": len(analysis.ambiguities or []),
                "created_at": self._isoformat(analysis.created_at),
            })
            add_edge(deal_node_id, analysis_node_id, "has_analysis")

        for generated_document in deal.generated_documents.all().order_by('-created_at'):
            generated_node_id = f"generated_document:{generated_document.id}"
            add_node(generated_node_id, "generated_document", generated_document.title, "ready" if generated_document.content else "queued", {
                "kind": generated_document.kind,
                "selected_deal_ids": generated_document.selected_deal_ids or [],
                "selected_document_ids": generated_document.selected_document_ids or [],
                "selected_chunk_ids": generated_document.selected_chunk_ids or [],
                "audit_log_id": generated_document.audit_log_id,
                "created_at": self._isoformat(generated_document.created_at),
            })
            add_edge(deal_node_id, generated_node_id, "generated")
            for document_id in generated_document.selected_document_ids or []:
                add_edge(generated_node_id, f"document:{document_id}", "used_document")
            for chunk_id in generated_document.selected_chunk_ids or []:
                add_edge(generated_node_id, chunk_node_ids_by_chunk_id.get(str(chunk_id), ""), "used_chunk")

        for relation in deal.vi_relations.select_related('company_profile').all():
            profile = relation.company_profile
            relation_type = relation.relation_type or "target"
            vi_node_id = f"vi:{profile.id}:{relation_type}"
            add_node(vi_node_id, "vi_competitor" if relation_type == "competitor" else "vi_target", profile.name or profile.registered_name, relation_type, {
                "profile_id": str(profile.id),
                "relation_type": relation_type,
                "registered_name": profile.registered_name,
                "cin": profile.cin,
                "sector": profile.sector,
                "industry": profile.industry,
                "website": profile.website,
                "total_funding": profile.total_funding,
                "city": profile.city,
                "created_at": self._isoformat(relation.created_at),
            })
            add_edge(deal_node_id, vi_node_id, "has_vi_profile")

        for context in deal.relationship_contexts.select_related('related_deal').all():
            context_node_id = f"relationship_context:{context.id}"
            label = context.related_deal.title if context.related_deal else context.relationship_type.replace("_", " ").title()
            add_node(context_node_id, "relationship_context", label, context.relationship_type, {
                "related_deal_id": str(context.related_deal_id) if context.related_deal_id else None,
                "notes": self._truncate_text(context.notes, 300),
                "selected_deal_ids": context.selected_deal_ids or [],
                "selected_document_ids": context.selected_document_ids or [],
                "selected_chunk_ids": context.selected_chunk_ids or [],
                "created_at": self._isoformat(context.created_at),
            })
            add_edge(deal_node_id, context_node_id, "has_relationship_context")
            for document_id in context.selected_document_ids or []:
                add_edge(context_node_id, f"document:{document_id}", "references_document")
            for chunk_id in context.selected_chunk_ids or []:
                add_edge(context_node_id, chunk_node_ids_by_chunk_id.get(str(chunk_id), ""), "references_chunk")

        embedded_chunk_count = sum(1 for chunk in chunks if chunk.embedding is not None)
        last_indexed_at = next((chunk.indexed_at for chunk in chunks if chunk.indexed_at), None)
        return Response({
            "summary": {
                "deal_id": str(deal.id),
                "document_count": len(documents),
                "indexed_document_count": sum(1 for document in documents if document.is_indexed),
                "chunk_count": len(chunks),
                "embedded_chunk_count": embedded_chunk_count,
                "vi_target_count": deal.vi_relations.filter(relation_type="target").count(),
                "vi_competitor_count": deal.vi_relations.filter(relation_type="competitor").count(),
                "analysis_count": len(analyses),
                "generated_document_count": deal.generated_documents.count(),
                "relationship_context_count": deal.relationship_contexts.count(),
                "last_indexed_at": self._isoformat(last_indexed_at),
                "graph_chunk_node_limit": graph_chunk_node_limit,
                "omitted_chunk_nodes": max(0, len(chunks) - rendered_chunk_count),
            },
            "nodes": nodes,
            "edges": edges,
        })

    @action(detail=False, methods=['get'])
    def dashboard_metrics(self, request):
        """
        Calculates aggregate metrics for the dashboard without loading all records.
        """
        queryset = Deal.objects.all()

        stage_counts = [
            {
                'stage': row['current_phase'] or 'Unassigned',
                'count': row['count'],
            }
            for row in queryset.values('current_phase')
            .annotate(count=Count('id'))
            .order_by('current_phase')
        ]
        fund_counts = [
            {
                'fund': (row['fund'] or '').strip() or 'UNASSIGNED',
                'count': row['count'],
            }
            for row in queryset.values('fund')
            .annotate(count=Count('id'))
            .order_by('fund')
        ]

        analysis_queryset = queryset.annotate(
            dashboard_has_analysis=Exists(
                DealAnalysis.objects.filter(deal_id=OuterRef('pk'))
            )
        )
        analysis_available = analysis_queryset.filter(
            dashboard_has_analysis=True
        ).count()
        analysis_running = analysis_queryset.filter(
            dashboard_has_analysis=False,
            processing_status='processing',
        ).count()
        analysis_failed = analysis_queryset.filter(
            dashboard_has_analysis=False,
            processing_status='failed',
        ).count()
        analysis_not_started = (
            queryset.count()
            - analysis_available
            - analysis_running
            - analysis_failed
        )

        total_value = 0.0
        invested_ytd = 0.0
        for current_phase, funding_ask in queryset.values_list('current_phase', 'funding_ask').iterator(chunk_size=1000):
            parsed_amount = self._parse_funding_ask(funding_ask)
            total_value += parsed_amount
            if current_phase in ['Invested', 'Portfolio']:
                invested_ytd += parsed_amount

        return Response({
            'totalDeals': queryset.count(),
            'activeDeals': queryset.exclude(current_phase__in=['Passed', 'Invested', 'Portfolio']).count(),
            'closedDeals': queryset.filter(current_phase__in=['Invested', 'Portfolio']).count(),
            'totalValue': total_value,
            'investedYTD': invested_ytd,
            'stageCounts': stage_counts,
            'fundCounts': fund_counts,
            'analysisCounts': {
                'available': analysis_available,
                'running': analysis_running,
                'failed': analysis_failed,
                'notStarted': analysis_not_started,
            },
        })

    @action(detail=False, methods=['get'], url_path='document-gaps')
    def document_gaps(self, request):
        """Return separate, actionable queues for folder and document gaps."""
        queryset = Deal.objects.annotate(
            dashboard_has_documents=Exists(DealDocument.objects.filter(deal_id=OuterRef('pk')))
        ).order_by('title')
        missing_folders = queryset.filter(Q(source_onedrive_id__isnull=True) | Q(source_onedrive_id=''))
        missing_documents = queryset.filter(dashboard_has_documents=False)

        def serialize(items):
            return [
                {
                    'id': str(deal.id),
                    'title': deal.title or 'Untitled deal',
                    'source_onedrive_id': deal.source_onedrive_id,
                }
                for deal in items[:50]
            ]

        return Response({
            'counts': {
                'missing_folders': missing_folders.count(),
                'missing_documents': missing_documents.count(),
            },
            'missing_folders': serialize(missing_folders),
            'missing_documents': serialize(missing_documents),
        })

    def get_serializer_class(self):
        # Use lightweight serializer for list views to reduce payload size
        if self.action == 'list':
            return DealListSerializer
        if self.action == 'retrieve':
            return DealDetailSerializer
        if self.action == 'heavy_fields':
            return DealHeavyFieldsSerializer
        return DealSerializer

    def _can_review_contradictions(self, request, deal):
        user = request.user
        if user.is_superuser or user.is_staff:
            return True
        profile = getattr(user, "profile", None)
        if profile is None or profile.is_disabled:
            return False
        return profile.is_admin or deal.responsibility.filter(id=profile.id).exists()

    @staticmethod
    def _receipt_date_state(deal):
        if deal.received_at:
            return 'CANONICAL'
        suggestions = list(deal.receipt_date_suggestions.all())
        active = [item for item in suggestions if item.status in {'PENDING', 'CONFLICT'}]
        if len({item.proposed_date for item in active}) > 1 or any(item.status == 'CONFLICT' for item in active):
            return 'CONFLICTING'
        if active:
            return 'SUGGESTED'
        if suggestions and all(item.status == 'REJECTED' for item in suggestions):
            return 'REJECTED'
        return 'UNKNOWN'

    @staticmethod
    def _serialize_receipt_suggestion(suggestion):
        reviewer = suggestion.reviewed_by
        reviewer_profile = getattr(reviewer, 'profile', None) if reviewer else None
        return {
            'id': str(suggestion.id),
            'proposed_date': suggestion.proposed_date.isoformat(),
            'source_type': suggestion.source_type,
            'source_id': suggestion.source_id,
            'evidence': suggestion.evidence,
            'confidence': suggestion.confidence,
            'status': suggestion.status,
            'review_note': suggestion.review_note,
            'reviewed_at': suggestion.reviewed_at.isoformat() if suggestion.reviewed_at else None,
            'reviewed_by': ({
                'id': reviewer.id,
                'name': getattr(reviewer_profile, 'name', None) or reviewer.get_username(),
            } if reviewer else None),
            'created_at': suggestion.created_at.isoformat(),
        }

    def _serialize_receipt_remediation(self, deal):
        return {
            'id': str(deal.id),
            'title': deal.title,
            'fund': deal.fund,
            'received_at': deal.received_at.isoformat() if deal.received_at else None,
            'updated_at': deal.updated_at.isoformat(),
            'date_state': self._receipt_date_state(deal),
            'suggestions': [
                self._serialize_receipt_suggestion(item)
                for item in deal.receipt_date_suggestions.all()
            ],
        }

    @action(detail=False, methods=['get'], url_path='receipt-date-remediation')
    def receipt_date_remediation_list(self, request):
        queryset = Deal.objects.filter(received_at__isnull=True).prefetch_related(
            'receipt_date_suggestions__reviewed_by__profile'
        )
        fund = request.query_params.get('fund')
        if fund:
            if fund not in {'FUND1', 'FUND2', 'FUND3'}:
                return Response({'error': 'fund must be FUND1, FUND2, or FUND3.'}, status=400)
            queryset = filter_by_fund_alias(queryset, fund)
        search = str(request.query_params.get('search') or '').strip()
        if search:
            queryset = queryset.filter(title__icontains=search)
        profile = getattr(request.user, 'profile', None)
        if not (request.user.is_superuser or request.user.is_staff or (profile and profile.is_admin)):
            if profile is None or profile.is_disabled:
                return Response({'error': 'An active analyst profile is required.'}, status=403)
            queryset = queryset.filter(responsibility=profile)
        rows = [self._serialize_receipt_remediation(deal) for deal in queryset.order_by('fund', 'title', 'id')]
        state_filter = request.query_params.get('status')
        if state_filter:
            valid = {'SUGGESTED', 'CONFLICTING', 'REJECTED', 'UNKNOWN'}
            if state_filter not in valid:
                return Response({'error': f'status must be one of {", ".join(sorted(valid))}.'}, status=400)
            rows = [row for row in rows if row['date_state'] == state_filter]
        counts = {
            state: sum(1 for row in rows if row['date_state'] == state)
            for state in ('SUGGESTED', 'CONFLICTING', 'REJECTED', 'UNKNOWN')
        }
        fund_counts = {
            value: sum(1 for row in rows if row['fund'] == value)
            for value in ('FUND1', 'FUND2', 'FUND3')
        }
        page = self.paginate_queryset(rows)
        response = self.get_paginated_response(page) if page is not None else Response({'count': len(rows), 'results': rows})
        response.data['counts'] = counts
        response.data['fund_counts'] = fund_counts
        return response

    @action(detail=True, methods=['post'], url_path='receipt-date-suggestions')
    def receipt_date_suggestions(self, request, pk=None):
        deal = self.get_object()
        if not self._can_review_contradictions(request, deal):
            return Response({'error': 'Only deal-responsible analysts or administrators can add date evidence.'}, status=403)
        if deal.received_at:
            return Response({'error': 'This deal already has a canonical receipt date.'}, status=409)
        source_type = str(request.data.get('source_type') or 'ANALYST').upper()
        if source_type == 'SOURCE_EMAIL':
            suggestion, created = ReceiptDateEvidenceService.propose_from_linked_email(deal)
            if suggestion is None:
                return Response({'error': 'No linked source email with a received timestamp was found.'}, status=400)
        else:
            proposed_date = parse_date(str(request.data.get('proposed_date') or ''))
            evidence_text = str(request.data.get('evidence') or '').strip()
            source_id = str(request.data.get('source_id') or '').strip()
            if proposed_date is None or not evidence_text or not source_id:
                return Response({'error': 'proposed_date, source_id, and evidence are required.'}, status=400)
            try:
                suggestion, created = ReceiptDateEvidenceService.propose(
                    deal=deal,
                    proposed_date=proposed_date,
                    source_type=source_type,
                    source_id=source_id,
                    evidence={'description': evidence_text, 'entered_by': request.user.id},
                    confidence=float(request.data.get('confidence', 1.0)),
                )
            except (TypeError, ValueError) as error:
                return Response({'error': str(error)}, status=400)
        return Response(
            {'created': created, 'suggestion': self._serialize_receipt_suggestion(suggestion)},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @action(
        detail=True,
        methods=['patch'],
        url_path=r'receipt-date-suggestions/(?P<suggestion_id>[^/.]+)',
    )
    def review_receipt_date_suggestion(self, request, pk=None, suggestion_id=None):
        decision = str(request.data.get('decision') or '').upper()
        if decision not in {'ACCEPT', 'REJECT'}:
            return Response({'error': 'decision must be ACCEPT or REJECT.'}, status=400)
        expected_updated_at = parse_datetime(str(request.data.get('expected_updated_at') or ''))
        if expected_updated_at is None:
            return Response({'error': 'expected_updated_at must be a valid ISO-8601 datetime.'}, status=400)
        with transaction.atomic():
            deal = Deal.objects.select_for_update().filter(pk=pk).first()
            if deal is None:
                return Response({'error': 'Deal was not found.'}, status=404)
            if not self._can_review_contradictions(request, deal):
                return Response({'error': 'Only deal-responsible analysts or administrators can review date evidence.'}, status=403)
            if deal.updated_at != expected_updated_at:
                return Response({'error': 'The deal changed after this evidence was loaded.', 'current_updated_at': deal.updated_at.isoformat()}, status=409)
            suggestion = DealReceiptDateSuggestion.objects.select_for_update().filter(
                id=suggestion_id,
                deal=deal,
            ).first()
            if suggestion is None:
                return Response({'error': 'Suggestion was not found for this deal.'}, status=404)
            if suggestion.status in {'ACCEPTED', 'REJECTED'}:
                return Response({'error': 'This suggestion has already been reviewed.'}, status=409)
            now = timezone.now()
            note = str(request.data.get('note') or '').strip()
            if decision == 'ACCEPT':
                if deal.received_at:
                    return Response({'error': 'This deal already has a canonical receipt date.'}, status=409)
                conflicts = DealReceiptDateSuggestion.objects.select_for_update().filter(
                    deal=deal,
                    status__in=['PENDING', 'CONFLICT'],
                ).exclude(proposed_date=suggestion.proposed_date)
                if conflicts.exists() and request.data.get('resolve_conflict') is not True:
                    return Response({'error': 'Conflicting candidate dates require explicit conflict resolution.'}, status=409)
                previous_date = deal.received_at
                deal.received_at = suggestion.proposed_date
                deal.save(update_fields=['received_at', 'updated_at'])
                suggestion.status = 'ACCEPTED'
                suggestion.reviewed_by = request.user
                suggestion.reviewed_at = now
                suggestion.review_note = note
                suggestion.save(update_fields=['status', 'reviewed_by', 'reviewed_at', 'review_note', 'updated_at'])
                conflicts.update(status='REJECTED', reviewed_by=request.user, reviewed_at=now, review_note='Rejected during explicit conflict resolution.')
                audit = DealReceiptDateAudit.objects.create(
                    deal=deal,
                    suggestion=suggestion,
                    previous_date=previous_date,
                    new_date=suggestion.proposed_date,
                    source_type=suggestion.source_type,
                    source_id=suggestion.source_id,
                    evidence=suggestion.evidence,
                    reviewer=request.user,
                )
                return Response({'deal': self._serialize_receipt_remediation(deal), 'audit_id': str(audit.id)})
            suggestion.status = 'REJECTED'
            suggestion.reviewed_by = request.user
            suggestion.reviewed_at = now
            suggestion.review_note = note
            suggestion.save(update_fields=['status', 'reviewed_by', 'reviewed_at', 'review_note', 'updated_at'])
            remaining = DealReceiptDateSuggestion.objects.filter(deal=deal, status__in=['PENDING', 'CONFLICT'])
            remaining.update(status=('CONFLICT' if remaining.values('proposed_date').distinct().count() > 1 else 'PENDING'))
        return Response({'deal': self._serialize_receipt_remediation(deal)})

    @action(detail=False, methods=['get'], url_path='fund-classification-summary')
    def fund_classification_summary(self, request):
        queryset = Deal.objects.all()
        user = request.user
        if not (user.is_superuser or user.is_staff):
            profile = getattr(user, 'profile', None)
            if profile is None or profile.is_disabled:
                return Response(
                    {'error': 'An active analyst profile is required.'},
                    status=status.HTTP_403_FORBIDDEN,
                )
            if not profile.is_admin:
                queryset = queryset.filter(responsibility=profile)
        fund = request.query_params.get('fund')
        if fund:
            if fund not in {'FUND1', 'FUND2', 'FUND3'}:
                return Response(
                    {'error': 'fund must be FUND1, FUND2, or FUND3.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            queryset = filter_by_fund_alias(queryset, fund)
        counts = {
            state: queryset.filter(fund_classification_state=state).count()
            for state in FundClassificationState.values
        }
        counts['total'] = sum(counts.values())
        return Response({'counts': counts, 'fund': fund})

    @action(detail=True, methods=['patch'], url_path='fund-classification')
    def fund_classification(self, request, pk=None):
        target_fund = str(request.data.get('fund') or '').upper()
        if target_fund not in {'FUND1', 'FUND2', 'FUND3'}:
            return Response(
                {'error': 'fund must be FUND1, FUND2, or FUND3.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        expected_updated_at = parse_datetime(str(request.data.get('expected_updated_at') or ''))
        if expected_updated_at is None:
            return Response(
                {'error': 'expected_updated_at must be a valid ISO-8601 datetime.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        source_id = str(request.data.get('source_id') or '').strip()

        with transaction.atomic():
            deal = Deal.objects.select_for_update().filter(pk=pk).first()
            if deal is None:
                return Response({'error': 'Deal was not found.'}, status=status.HTTP_404_NOT_FOUND)
            if not self._can_review_contradictions(request, deal):
                return Response(
                    {'error': 'Only deal-responsible analysts or administrators can review fund classification.'},
                    status=status.HTTP_403_FORBIDDEN,
                )
            if deal.updated_at != expected_updated_at:
                return Response(
                    {
                        'error': 'The deal changed after this classification was loaded.',
                        'current_updated_at': deal.updated_at.isoformat(),
                    },
                    status=status.HTTP_409_CONFLICT,
                )
            deal.fund = target_fund
            deal.fund_classification_state = FundClassificationState.EXPLICIT
            deal.fund_classification_source_type = FundClassificationSourceType.ANALYST_REVIEW
            deal.fund_classification_source_id = source_id or f'analyst-review:{deal.id}'
            deal.fund_classification_reviewed_by = request.user
            deal.fund_classification_reviewed_at = timezone.now()
            deal.save(update_fields=[
                'fund', 'fund_classification_state',
                'fund_classification_source_type', 'fund_classification_source_id',
                'fund_classification_reviewed_by', 'fund_classification_reviewed_at',
                'updated_at',
            ])
        return Response(DealListSerializer(deal, context={'request': request}).data)

    @staticmethod
    def _pass_reason_remediation_queryset(user):
        queryset = Deal.objects.filter(current_phase=DealPhase.PASSED).annotate(
            normalized_pass_reason=Trim(
                Coalesce('reasons_for_passing', Value(''), output_field=CharField())
            )
        ).filter(normalized_pass_reason='')
        if user.is_superuser or user.is_staff:
            return queryset
        profile = getattr(user, 'profile', None)
        if profile is None or profile.is_disabled:
            return queryset.none()
        if profile.is_admin:
            return queryset
        return queryset.filter(responsibility=profile)

    @staticmethod
    def _serialize_pass_reason_remediation(deal):
        latest_audit = deal.pass_reason_remediation_audits.select_related(
            'actor', 'actor__profile'
        ).first()
        actor = None
        if latest_audit and latest_audit.actor:
            profile = getattr(latest_audit.actor, 'profile', None)
            actor = {
                'id': latest_audit.actor_id,
                'name': getattr(profile, 'name', None) or latest_audit.actor.get_username(),
                'email': getattr(profile, 'email', None) or latest_audit.actor.email,
            }
        return {
            'id': str(deal.id),
            'title': deal.title,
            'fund': deal.fund,
            'current_phase': deal.current_phase,
            'reasons_for_passing': deal.reasons_for_passing,
            'updated_at': deal.updated_at.isoformat(),
            'last_remediation': ({
                'actor': actor,
                'created_at': latest_audit.created_at.isoformat(),
            } if latest_audit else None),
        }

    @action(
        detail=False,
        methods=['get'],
        url_path='pass-reason-remediation',
        url_name='pass-reason-remediation-list',
    )
    def pass_reason_remediation_list(self, request):
        queryset = self._pass_reason_remediation_queryset(request.user)
        fund = request.query_params.get('fund')
        if fund and fund not in {'FUND1', 'FUND2'}:
            return Response(
                {'error': 'fund must be FUND1 or FUND2.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        queryset = queryset.filter(fund__in=['FUND1', 'FUND2'])
        counts = {
            'FUND1': queryset.filter(fund='FUND1').count(),
            'FUND2': queryset.filter(fund='FUND2').count(),
        }
        counts['total'] = counts['FUND1'] + counts['FUND2']
        if fund:
            queryset = queryset.filter(fund=fund)
        search = (request.query_params.get('search') or '').strip()
        if search:
            queryset = queryset.filter(title__icontains=search)
        queryset = queryset.order_by('fund', 'title', 'id')
        page = self.paginate_queryset(queryset)
        rows = [
            self._serialize_pass_reason_remediation(deal)
            for deal in (page if page is not None else queryset)
        ]
        if page is None:
            return Response({'count': len(rows), 'results': rows, 'counts': counts})
        response = self.get_paginated_response(rows)
        response.data['counts'] = counts
        return response

    @action(
        detail=True,
        methods=['patch'],
        url_path='pass-reason-remediation',
        url_name='pass-reason-remediation-detail',
    )
    def pass_reason_remediation(self, request, pk=None):
        reason = str(request.data.get('reason') or '').strip()
        if not reason:
            return Response(
                {'error': 'A non-whitespace reason is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        expected_raw = request.data.get('expected_updated_at')
        expected_updated_at = parse_datetime(str(expected_raw or ''))
        if expected_updated_at is None:
            return Response(
                {'error': 'expected_updated_at must be a valid ISO-8601 datetime.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            deal = Deal.objects.select_for_update().filter(pk=pk).first()
            if deal is None:
                return Response(
                    {'error': 'Deal was not found.'},
                    status=status.HTTP_404_NOT_FOUND,
                )
            allowed = self._pass_reason_remediation_queryset(request.user).filter(pk=deal.pk).exists()
            if not allowed:
                profile = getattr(request.user, 'profile', None)
                can_access = (
                    request.user.is_superuser
                    or request.user.is_staff
                    or bool(profile and not profile.is_disabled and (
                        profile.is_admin or deal.responsibility.filter(id=profile.id).exists()
                    ))
                )
                if not can_access:
                    return Response(
                        {'error': 'You do not have permission to remediate this deal.'},
                        status=status.HTTP_403_FORBIDDEN,
                    )
                return Response(
                    {'error': 'This deal is no longer eligible for pass-reason remediation.'},
                    status=status.HTTP_409_CONFLICT,
                )
            if deal.updated_at != expected_updated_at:
                return Response(
                    {
                        'error': 'The deal changed after this queue entry was loaded.',
                        'current_updated_at': deal.updated_at.isoformat(),
                    },
                    status=status.HTTP_409_CONFLICT,
                )

            previous_reason = deal.reasons_for_passing
            serializer = DealSerializer(
                deal,
                data={'reasons_for_passing': reason},
                partial=True,
                context={'request': request},
            )
            serializer.is_valid(raise_exception=True)
            updated = serializer.save()
            audit = DealPassReasonRemediationAudit.objects.create(
                deal=updated,
                actor=request.user,
                previous_reason=previous_reason,
                new_reason=reason,
                deal_updated_at=updated.updated_at,
            )

        return Response({
            'deal': self._serialize_pass_reason_remediation(updated),
            'audit': {
                'id': str(audit.id),
                'actor_id': audit.actor_id,
                'created_at': audit.created_at.isoformat(),
            },
        })

    @action(detail=True, methods=["get", "post"])
    def research_discovery(self, request, pk=None):
        deal = self.get_object()
        from .services.research_discovery import ResearchDiscoveryCoordinator

        if request.method == "POST":
            if not self._can_review_contradictions(request, deal):
                return Response(
                    {
                        "error": (
                            "Only deal-responsible analysts or administrators "
                            "can refresh research discovery."
                        )
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )
            run, created = ResearchDiscoveryCoordinator.enqueue(
                deal=deal,
                trigger=SectorResearchDiscoveryRun.Trigger.MANUAL,
                requested_by=request.user,
            )
            return Response(
                {
                    "created": created,
                    "run": SectorResearchDiscoveryRunSerializer(run).data,
                },
                status=(
                    status.HTTP_202_ACCEPTED
                    if created
                    else status.HTTP_200_OK
                ),
            )

        valid_document_types = {
            choice
            for choice, _label in SectorResearchRecommendation.DocumentType.choices
        }
        valid_accessibility = {
            choice
            for choice, _label in SectorResearchRecommendation.Accessibility.choices
        }
        document_type = request.query_params.get("document_type")
        accessibility = request.query_params.get("accessibility")
        if document_type and document_type not in valid_document_types:
            return Response(
                {"error": "Invalid document_type filter."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if accessibility and accessibility not in valid_accessibility:
            return Response(
                {"error": "Invalid accessibility filter."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        recommendations = deal.research_recommendations.all()
        if document_type:
            recommendations = recommendations.filter(
                document_type=document_type
            )
        if accessibility:
            recommendations = recommendations.filter(
                accessibility=accessibility
            )
        latest_run = deal.research_discovery_runs.first()
        current_hash = ResearchDiscoveryCoordinator.context_hash(deal)
        return Response(
            {
                "run": (
                    SectorResearchDiscoveryRunSerializer(latest_run).data
                    if latest_run
                    else None
                ),
                "context_stale": bool(
                    latest_run and latest_run.context_hash != current_hash
                ),
                "recommendations": SectorResearchRecommendationSerializer(
                    recommendations[:100],
                    many=True,
                ).data,
            }
        )

    @action(
        detail=True,
        methods=["post"],
        url_path=r"research-acquisition/(?P<recommendation_id>[^/.]+)/acquire",
    )
    def acquire_research(self, request, pk=None, recommendation_id=None):
        deal = self.get_object()
        if not self._can_review_contradictions(request, deal):
            return Response({"error": "Only deal-responsible analysts or administrators can acquire research."}, status=403)
        if request.data.get("approved") is not True:
            return Response({"error": "Explicit approval is required before network and storage work begins."}, status=400)
        recommendation = SectorResearchRecommendation.objects.filter(
            id=recommendation_id,
            deal=deal,
        ).first()
        if recommendation is None:
            return Response({"error": "Research recommendation was not found for this deal."}, status=404)
        if recommendation.accessibility != SectorResearchRecommendation.Accessibility.AVAILABLE:
            return Response({"error": "Only currently verified public recommendations can be acquired."}, status=409)
        with transaction.atomic():
            acquisition, created = SectorResearchAcquisition.objects.select_for_update().get_or_create(
                recommendation=recommendation,
                defaults={
                    "deal": deal,
                    "approved_by": request.user,
                    "source_url": recommendation.canonical_url,
                },
            )
            active = {
                SectorResearchAcquisition.Status.QUEUED,
                SectorResearchAcquisition.Status.DOWNLOADING,
                SectorResearchAcquisition.Status.ATTACHED,
                SectorResearchAcquisition.Status.EXTRACTING,
            }
            if not created and acquisition.status in active | {
                SectorResearchAcquisition.Status.COMPLETED,
                SectorResearchAcquisition.Status.PARTIAL,
            }:
                return Response({"created": False, "acquisition": SectorResearchAcquisitionSerializer(acquisition).data})
            from ai_orchestrator.services.runtime import AIRuntimeService
            audit = AIRuntimeService.create_audit_log(
                source_type="sector_research_acquisition",
                source_id=str(recommendation.id),
                context_label=f"Approved research acquisition — {recommendation.title}",
                status="PENDING",
                is_success=False,
                requested_by=request.user,
                source_metadata={
                    "deal_id": str(deal.id),
                    "recommendation_id": str(recommendation.id),
                    "source_url": recommendation.canonical_url,
                    "explicit_approval": True,
                },
            )
            acquisition.status = SectorResearchAcquisition.Status.QUEUED
            acquisition.approved_by = request.user
            acquisition.audit_log = audit
            acquisition.document = None
            acquisition.error_code = ""
            acquisition.error_detail = ""
            acquisition.completed_at = None
            acquisition.save()
        from .tasks import acquire_sector_research_task
        try:
            queued = acquire_sector_research_task.delay(str(acquisition.id))
            acquisition.celery_task_id = str(queued.id or "")
            acquisition.save(update_fields=["celery_task_id", "updated_at"])
        except Exception as error:
            acquisition.status = SectorResearchAcquisition.Status.FAILED
            acquisition.error_code = "QUEUE_FAILED"
            acquisition.error_detail = str(error)
            acquisition.completed_at = timezone.now()
            acquisition.save()
        return Response(
            {"created": True, "acquisition": SectorResearchAcquisitionSerializer(acquisition).data},
            status=202 if acquisition.status == SectorResearchAcquisition.Status.QUEUED else 503,
        )

    @action(
        detail=True,
        methods=["get", "patch"],
        url_path=r"research-acquisitions/(?P<acquisition_id>[^/.]+)",
    )
    def research_acquisition(self, request, pk=None, acquisition_id=None):
        deal = self.get_object()
        if not self._can_review_contradictions(request, deal):
            return Response({"error": "Only deal-responsible analysts or administrators can inspect acquisitions."}, status=403)
        acquisition = SectorResearchAcquisition.objects.select_related("document", "audit_log").filter(
            id=acquisition_id,
            deal=deal,
        ).first()
        if acquisition is None:
            return Response({"error": "Research acquisition was not found for this deal."}, status=404)
        if request.method == "PATCH":
            with transaction.atomic():
                acquisition = SectorResearchAcquisition.objects.select_for_update().get(pk=acquisition.pk)
                action_name = str(request.data.get("action") or "").upper()
                if action_name != "CANCEL" or acquisition.status != SectorResearchAcquisition.Status.QUEUED:
                    return Response({"error": "Only a queued acquisition can be cancelled."}, status=409)
                acquisition.status = SectorResearchAcquisition.Status.CANCELLED
                acquisition.completed_at = timezone.now()
                acquisition.save(update_fields=["status", "completed_at", "updated_at"])
        return Response(SectorResearchAcquisitionSerializer(acquisition).data)

    @action(detail=True, methods=["get", "patch"])
    def contradictions(self, request, pk=None):
        deal = self.get_object()
        queryset = deal.contradictions.select_related(
            "reviewed_by",
            "audit_log",
        ).all()

        if request.method == "GET":
            review_status = request.query_params.get("status")
            classification = request.query_params.get("classification")
            if review_status:
                valid_statuses = {
                    choice
                    for choice, _label in DealContradiction.ReviewStatus.choices
                }
                if review_status not in valid_statuses:
                    return Response(
                        {"error": "status must be UNREVIEWED, CONFIRMED, or DISMISSED."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                queryset = queryset.filter(review_status=review_status)
            if classification:
                queryset = queryset.filter(classification=classification)
            return Response(
                DealContradictionSerializer(
                    queryset,
                    many=True,
                    context={"request": request},
                ).data
            )

        if not self._can_review_contradictions(request, deal):
            return Response(
                {"error": "Only deal-responsible analysts or administrators can review contradictions."},
                status=status.HTTP_403_FORBIDDEN,
            )
        contradiction_id = request.data.get("id")
        if not contradiction_id:
            return Response(
                {"error": "id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        contradiction = queryset.filter(id=contradiction_id).first()
        if contradiction is None:
            return Response(
                {"error": "Contradiction was not found for this deal."},
                status=status.HTTP_404_NOT_FOUND,
            )
        serializer = DealContradictionSerializer(
            contradiction,
            data=request.data,
            partial=True,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)
    
    @action(detail=True, methods=['get'])
    def heavy_fields(self, request, pk=None):
        """
        Retrieve heavy fields (thinking, extracted_text, analysis_history)
        lazily for forensic details.
        """
        deal = self.get_object()
        serializer = self.get_serializer(deal)
        data = serializer.data

        include_extracted_text = request.query_params.get('include_extracted_text', 'true').lower() == 'true'
        include_thinking = request.query_params.get('include_thinking', 'true').lower() == 'true'

        if not include_extracted_text:
            data.pop('extracted_text', None)

        if not include_thinking:
            data.pop('thinking', None)

        return Response(data)

    @action(detail=True, methods=['patch'])
    def update_analysis_report(self, request, pk=None):
        """
        Persist analyst edits to a stored analysis report, or to deal_summary
        when the report is only the legacy fallback summary.
        """
        deal = self.get_object()
        report = request.data.get('report')
        if not isinstance(report, str):
            return Response({"error": "report must be a string"}, status=400)

        version = request.data.get('version')
        analysis = None
        if version not in (None, ''):
            try:
                analysis = deal.analyses.filter(version=int(version)).order_by('-created_at').first()
            except (TypeError, ValueError):
                return Response({"error": "version must be a number"}, status=400)

        if analysis:
            analysis_json = analysis.analysis_json if isinstance(analysis.analysis_json, dict) else {}
            analysis_json['analyst_report'] = report
            canonical_snapshot = analysis_json.get('canonical_snapshot')
            if isinstance(canonical_snapshot, dict):
                canonical_snapshot['analyst_report'] = report
                analysis_json['canonical_snapshot'] = canonical_snapshot
            analysis.analysis_json = analysis_json
            analysis.save(update_fields=['analysis_json'])

            if deal.latest_analysis and deal.latest_analysis.id == analysis.id:
                deal.deal_summary = report
                deal.save(update_fields=['deal_summary'])
        else:
            deal.deal_summary = report
            deal.save(update_fields=['deal_summary'])
            from work_items.services import sync_deal_suggestions
            sync_deal_suggestions(deal, None)

        deal.refresh_from_db()
        return Response({
            "status": "saved",
            "deal_summary": deal.deal_summary,
            "current_analysis": deal.current_analysis,
            "analysis_history": deal.analysis_history,
        })

    @action(detail=True, methods=['post'])
    def rewrite_analysis_section(self, request, pk=None):
        """
        Preview one analysis report section rewrite, then persist only after a
        separate signed confirmation request.
        """
        deal = self.get_object()
        section_markdown = request.data.get('section_markdown')
        section_title = request.data.get('section_title')
        instruction = request.data.get('instruction')
        full_report = request.data.get('full_report')

        for field_name, value in (
            ('section_markdown', section_markdown),
            ('section_title', section_title),
            ('instruction', instruction),
            ('full_report', full_report),
        ):
            if not isinstance(value, str):
                return Response({"error": f"{field_name} must be a string"}, status=400)

        section_markdown = section_markdown.strip()
        section_title = section_title.strip() or "Untitled Section"
        instruction = instruction.strip()
        full_report = full_report.strip()

        if not section_markdown:
            return Response({"error": "section_markdown cannot be empty"}, status=400)
        if not instruction:
            return Response({"error": "instruction cannot be empty"}, status=400)

        version = request.data.get('version')
        if version not in (None, ''):
            try:
                int(version)
            except (TypeError, ValueError):
                return Response({"error": "version must be a number"}, status=400)

        try:
            import hashlib
            from django.core import signing
            from deals.services.analysis_section_rewrite import AnalysisSectionRewriteService

            rewrite_service = AnalysisSectionRewriteService()
            confirmation_token = request.data.get("confirmation_token")
            if confirmation_token:
                try:
                    confirmation = signing.loads(
                        confirmation_token,
                        salt="analysis-section-rewrite",
                        max_age=3600,
                    )
                except signing.BadSignature:
                    return Response(
                        {"error": "The rewrite confirmation is invalid or expired."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                expected = {
                    "deal_id": str(deal.id),
                    "version": str(version or ""),
                    "section_title": section_title,
                    "report_sha256": hashlib.sha256(full_report.encode("utf-8")).hexdigest(),
                }
                if any(confirmation.get(key) != value for key, value in expected.items()):
                    return Response(
                        {"error": "The report or section changed after the preview. Generate a new rewrite preview."},
                        status=status.HTTP_409_CONFLICT,
                    )
                rewritten = str(confirmation.get("section_markdown") or "").strip()
                updated_report = rewrite_service.replace_section(full_report, section_title, rewritten)
                rewrite_service.persist(deal=deal, full_report=updated_report, version=version)
                return Response({
                    "section_markdown": rewritten,
                    "full_report": updated_report,
                    "persisted": True,
                    "confirmation_required": False,
                })

            rewritten = rewrite_service.rewrite(
                deal=deal,
                section_title=section_title,
                section_markdown=section_markdown,
                instruction=instruction,
                full_report=full_report,
                version=version,
            )
            token = signing.dumps(
                {
                    "deal_id": str(deal.id),
                    "version": str(version or ""),
                    "section_title": section_title,
                    "report_sha256": hashlib.sha256(full_report.encode("utf-8")).hexdigest(),
                    "section_markdown": rewritten,
                },
                salt="analysis-section-rewrite",
                compress=True,
            )
            return Response({
                "section_markdown": rewritten,
                "full_report": full_report,
                "persisted": False,
                "confirmation_required": True,
                "confirmation_token": token,
            })
        except Exception as exc:
            logger.exception("Failed to rewrite analysis section for deal %s", deal.id)
            return Response({"error": str(exc)}, status=500)
    
    def create(self, request, *args, **kwargs):
        """
        Custom create to support session-based deal initialization (from OneDrive or Email).
        """
        session_id = request.data.get('sessionId')
        if not session_id:
            return super().create(request, *args, **kwargs)

        try:
            from ai_orchestrator.models import AIAuditLog
            from .services.folder_analysis import FolderAnalysisService

            # 1. Resolve Session/Audit Log
            audit_log = AIAuditLog.objects.filter(id=session_id).first()
            if not audit_log:
                return Response({"error": "Invalid session or audit log ID"}, status=400)

            # 2. Validate before opening the atomic persistence boundary.
            serializer = self.get_serializer(data=request.data)
            serializer.is_valid(raise_exception=True)

            with transaction.atomic():
                deal = serializer.save()
                result = FolderAnalysisService.confirm_deal_from_session(session_id, deal)

                if result.get("error"):
                    transaction.set_rollback(True)
                    return Response(result, status=400)

                confirmed_deal_id = result.get("deal_id")
                if confirmed_deal_id and str(confirmed_deal_id) != str(deal.id):
                    deal = Deal.objects.get(id=confirmed_deal_id)

                if audit_log.source_type == 'email' and audit_log.source_id:
                    from microsoft.models import Email

                    source_email = Email.objects.select_for_update().filter(id=audit_log.source_id).first()
                    if not source_email:
                        raise ValueError(f"Source email {audit_log.source_id} does not exist")
                    thread = Email.objects.filter(id=source_email.id)
                    if source_email.conversation_id:
                        thread = Email.objects.filter(conversation_id=source_email.conversation_id)
                    thread.update(deal=deal)
                    source_email.is_processed = True
                    source_email.save(update_fields=["is_processed"])

            # 5. Return the finalized deal
            return Response(self.get_serializer(deal).data, status=status.HTTP_201_CREATED)

        except Exception as e:
            import traceback
            logger.error(f"Failed to confirm deal from session {session_id}: {traceback.format_exc()}")
            return Response({"error": str(e)}, status=500)

    def perform_create(self, serializer):
        deal = serializer.save()
        DealCreationService.process_deal_creation(deal, serializer.validated_data, self.request.user)

    @action(detail=True, methods=['patch'])
    def connect_onedrive(self, request, pk=None):
        """
        Manually link a OneDrive folder to an existing deal.
        """
        from microsoft.services.graph_service import DMS_USER_EMAIL

        deal = self.get_object()
        folder_id = request.data.get('source_onedrive_id')
        drive_id = request.data.get('source_drive_id')
        
        if not folder_id or not drive_id:
            return Response({"error": "source_onedrive_id and source_drive_id are required"}, status=400)
            
        deal.source_onedrive_id = folder_id
        deal.source_drive_id = drive_id
        deal.save(update_fields=['source_onedrive_id', 'source_drive_id'])

        # 1. Pickup existing analyzed files
        from deals.services.vdr_sync import VDRSyncService
        user_email = getattr(request.user, 'email', None) or DMS_USER_EMAIL
        linked_count = VDRSyncService.sync_existing_analyses_to_folder(deal, user_email=user_email)
        
        # 2. Synchronously traverse the folder tree to make it "instant" for the VDR dialog
        try:
            tree_count = FolderAnalysisService.persist_folder_tree(
                deal=deal, 
                folder_id=folder_id, 
                drive_id=drive_id, 
                user_email=DMS_USER_EMAIL
            )
            message = f"OneDrive folder linked. {tree_count} items discovered."
        except Exception as e:
            logger.error(f"Synchronous traversal failed: {e}")
            message = "OneDrive folder linked, but traversal failed. Folder view may be empty."

        return Response({
            "status": "success",
            "message": f"{message} Picked up {linked_count} existing analyses.",
            "source_onedrive_id": deal.source_onedrive_id,
            "source_drive_id": deal.source_drive_id,
            "linked_count": linked_count
        })

    @extend_schema(
        summary="Get deals grouped by priority",
        description="Retrieve all deals grouped by their priority level.",
        tags=["Deals"],
        responses={200: DealListSerializer(many=True)},
    )
    @action(detail=False, methods=['get'])
    def by_priority(self, request):
        # Group deals by priority level for dashboard/analytics views
        try:
            deals = self.get_queryset()
            grouped = {}
            # Iterate through all possible priority choices to ensure all groups are present
            for priority, _ in Deal._meta.get_field('priority').choices:
                grouped[priority] = DealListSerializer(
                    deals.filter(priority=priority),
                    many=True
                ).data
            return Response(grouped, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(f"Error in by_priority: {str(e)}")
            return Response(
                {
                    'error': 'Failed to group deals by priority',
                    'details': str(e)
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=True, methods=['post'])
    def transition_phase(self, request, pk=None):
        """
        Transitions a deal to a new phase and logs the rationale.
        """
        deal = self.get_object()
        try:
            result = DealFlowService.transition_phase(
                deal=deal,
                to_phase=request.data.get('to_phase'),
                rationale=request.data.get('rationale'),
                request_user=request.user
            )
        except DealFlowValidationError as exc:
            return Response({"reason": [str(exc)]}, status=status.HTTP_400_BAD_REQUEST)
        return Response(result)

    @action(detail=True, methods=['post'])
    def update_flow_state(self, request, pk=None):
        """
        Unified endpoint for the 18-stage interactive deal flow.
        Accepts `active_stage`, `decisions_update` (dict), and optional `reason`.
        """
        deal = self.get_object()
        try:
            result = DealFlowService.update_flow_state(
                deal=deal,
                active_stage=request.data.get('active_stage'),
                decisions_update=request.data.get('decisions_update'),
                reason=request.data.get('reason'),
                rejection_stage_id=request.data.get('rejection_stage_id'),
                request_user=request.user
            )
        except DealFlowValidationError as exc:
            return Response({"reason": [str(exc)]}, status=status.HTTP_400_BAD_REQUEST)
        return Response(result)

    @action(detail=True, methods=['get'])
    def get_paginated_extracted_text(self, request, pk=None):
        """
        Returns a slice of the extracted_text to avoid sending massive payloads.
        Query params: offset (int), limit (int, default 50000)
        """
        deal = self.get_object()
        offset = int(request.query_params.get('offset', 0))
        limit = int(request.query_params.get('limit', 50000))
        
        full_text = deal.extracted_text or ""
        total_length = len(full_text)
        
        text_slice = full_text[offset : offset + limit]
        
        return Response({
            "text": text_slice,
            "next_offset": offset + limit if offset + limit < total_length else None,
            "total_length": total_length
        })

    @action(detail=False, methods=['post'])
    def analyze_folder(self, request):
        """
        Kicks off an asynchronous folder analysis. Returns a task_id.
        """
        folder_id = request.data.get('folder_id')
        folder_name = request.data.get('folder_name', folder_id)
        drive_id = request.data.get('drive_id')
        
        if not folder_id:
            return Response({"error": "folder_id is required"}, status=400)
            
        result = FolderAnalysisService.queue_folder_analysis(folder_id, folder_name, drive_id)
        return Response(result)

    @action(detail=False, methods=['post'])
    def analyze_selection(self, request):
        """
        Kicks off AI extraction based on specific user-selected file IDs.
        """
        session_id = request.data.get('session_id')
        selected_file_ids = request.data.get('selected_file_ids', [])
        
        if not session_id or not selected_file_ids:
            return Response({"error": "session_id and selected_file_ids are required"}, status=400)
            
        try:
            result = FolderAnalysisService.trigger_selection_analysis(session_id, selected_file_ids)
            if "error" in result:
                return Response(result, status=400)
            return Response(result)
        except Exception as e:
            logger.error(f"Analyze selection failed: {str(e)}")
            return Response({"error": str(e)}, status=500)

    @action(detail=False, methods=['get'], url_path='task-status/(?P<task_id>[^/.]+)')
    def task_status(self, request, task_id=None):
        """
        Polls the status of an AI analysis task.
        """
        result = FolderAnalysisService.get_task_status(task_id)
        if result.get("status") == "FAILURE" or "error" in result:
            return Response(result, status=500)
        return Response(result)

    @action(detail=False, methods=['post'])
    def create_from_audit_log(self, request):
        """
        Kicks off the confirmation step for an existing audit log.
        Re-caches the session data so it can be confirmed via standard confirm_folder_deal.
        """
        log_id = request.data.get('audit_log_id')
        if not log_id:
            return Response({"error": "audit_log_id is required"}, status=400)
        
        result = FolderAnalysisService.create_session_from_audit_log(log_id)
        if "error" in result:
            return Response(result, status=400)
        return Response(result)

    @action(detail=False, methods=['post'])
    def confirm_selection_analysis(self, request):
        """
        Runs the final Qwen analysis on the approved subset of preflight-passed files.
        """
        session_id = request.data.get('session_id')
        selected_file_ids = request.data.get('selected_file_ids', [])

        if not session_id or not selected_file_ids:
            return Response({"error": "session_id and selected_file_ids are required"}, status=400)

        result = FolderAnalysisService.confirm_selection_analysis(session_id, selected_file_ids)
        if "error" in result:
            return Response(result, status=400)
        return Response(result)

    @action(detail=False, methods=['post'])
    def confirm_folder_deal(self, request):
        """
        Creates the Deal from the preliminary analysis.
        """
        session_id = request.data.get('session_id')
        deal_data = request.data.get('deal_data', {})
        
        if not session_id or not deal_data:
            return Response({"error": "session_id and deal_data are required"}, status=400)
            
        # 1. Create the Deal
        serializer = self.get_serializer(data=deal_data)
        if serializer.is_valid():
            deal = serializer.save()
            result = FolderAnalysisService.confirm_deal_from_session(session_id, deal)
            if "error" in result:
                deal.delete()
                return Response(result, status=400)
            status_code = 200 if result.get("message") == "Deal already created from this analysis session." else 201
            return Response(result, status=status_code)
            
        return Response(serializer.errors, status=400)

    @action(detail=True, methods=['post'])
    def start_vdr_processing(self, request, pk=None):
        """
        Starts deferred VDR processing for a deal linked to a OneDrive folder.
        """
        deal = self.get_object()
        result = FolderAnalysisService.trigger_vdr_processing(deal)
        if "error" in result:
            return Response(result, status=400)
        return Response(result)

    @action(detail=True, methods=['post'])
    def analyze_additional_documents(self, request, pk=None):
        """
        Updates the AI Summary (V2 Analysis) using the existing analysis and newly selected documents.
        Enforces a maximum of 5 documents at once.
        """
        deal = self.get_object()
        document_ids = request.data.get('document_ids', [])
        
        if not document_ids:
            return Response({"error": "No document IDs provided"}, status=400)
            
        if len(document_ids) > 5:
            return Response({"error": "Neural limit exceeded: Maximum 5 documents can be analyzed per incremental batch."}, status=400)
            
        docs = deal.documents.filter(id__in=document_ids)
        if not docs.exists():
            return Response({"error": "No matching documents found for this deal"}, status=400)
            
        from .tasks import analyze_additional_documents_async
        from ai_orchestrator.models import AIAuditLog, AIPersonality, AISkill
        
        # 1. Create a PENDING audit log immediately for visibility and cancellation support
        personality = AIPersonality.objects.filter(is_default=True).first()
        skill = AISkill.objects.filter(name='vdr_incremental_analysis').first()
        if not skill:
            return Response({"error": "AI skill 'vdr_incremental_analysis' is not configured."}, status=500)
        
        audit_log = AIRuntimeService.create_audit_log(
            source_type='vdr_incremental_analysis',
            source_id=str(deal.id),
            context_label=f"Incremental Analysis: {deal.title}",
            personality=personality,
            skill=skill,
            status='PENDING',
            is_success=False,
            system_prompt="Queued for incremental forensic analysis...",
            user_prompt=f"Analyzing {docs.count()} new documents for Deal: {deal.title}",
        )

        # 2. Trigger async task
        task = analyze_additional_documents_async.apply_async(
            kwargs={
                'deal_id': str(deal.id),
                'document_ids': document_ids,
                'audit_log_id': str(audit_log.id)
            },
            queue='high_priority'
        )
        
        audit_log.celery_task_id = task.id
        audit_log.save()
        
        return Response({
            "task_id": task.id,
            "audit_log_id": str(audit_log.id),
            "status": "queued",
            "message": f"Incremental analysis queued for {docs.count()} documents."
        })

    @action(detail=True, methods=['post'])
    def upload_document(self, request, pk=None):
        """
        Manually upload a file to the deal's VDR.
        """
        deal = self.get_object()
        file_obj = request.FILES.get('file')
        
        if not file_obj:
            return Response({"error": "No file provided"}, status=400)
            
        try:
            from ai_orchestrator.services.document_processor import DocumentProcessorService
            from ai_orchestrator.services.embedding_processor import EmbeddingService
            from ai_orchestrator.services.ai_processor import AIProcessorService
            from django.utils import timezone
            
            doc_processor = DocumentProcessorService()
            embed_service = EmbeddingService()
            ai_service = AIProcessorService()
            
            file_content = file_obj.read()
            file_name = file_obj.name
            
            from .models import DocumentType
            doc_type = DocumentType.OTHER
            name_lower = file_name.lower()
            if any(k in name_lower for k in ['financial', 'mis', 'model', 'projection']): 
                doc_type = DocumentType.FINANCIALS
            elif any(k in name_lower for k in ['legal', 'sha', 'ssa', 'term sheet']): 
                doc_type = DocumentType.LEGAL
            elif any(k in name_lower for k in ['teaser', 'deck', 'pitch', 'im']): 
                doc_type = DocumentType.PITCH_DECK
            elif any(k in name_lower for k in ['annual report', 'industry report', 'market report', 'sector report']):
                doc_type = DocumentType.MEMO
                
            extraction = doc_processor.get_extraction_result(file_content, file_name)
            extracted_text = (extraction.get("raw_extracted_text") or extraction.get("text") or "").strip()
            normalized_text = (extraction.get("normalized_text") or extraction.get("text") or extracted_text).strip()
            
            from .models import DealDocument
            doc = DealDocument.objects.create(
                deal=deal,
                title=file_name,
                document_type=doc_type,
                extracted_text=extracted_text,
                normalized_text=normalized_text,
                is_indexed=False,
                is_ai_analyzed=False,
                extraction_mode=extraction.get("mode"),
                transcription_status="complete" if normalized_text else "failed",
                chunking_status="not_chunked",
                last_transcribed_at=timezone.now() if normalized_text else None,
                uploaded_by=request.user.profile if hasattr(request.user, 'profile') else None
            )

            if normalized_text:
                DocumentArtifactService.ensure_document_artifact(doc, ai_service=ai_service, force=True)
            
            if normalized_text and len(normalized_text.strip()) > 50:
                aggregate_text = doc.normalized_text or normalized_text
                new_context = f"\n\n--- MANUAL DOCUMENT: {file_name} ---\n{aggregate_text}"
                deal.extracted_text = (deal.extracted_text or "") + new_context
                deal.save(update_fields=['extracted_text'])
                
                embed_service.vectorize_document(doc)
                
            from .serializers import DealDocumentSerializer
            return Response({
                "status": "success",
                "document": DealDocumentSerializer(doc).data
            })
            
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Manual upload failed: {str(e)}")
            return Response({"error": str(e)}, status=500)

    @action(detail=True, methods=['post'])
    def fetch_competitors(self, request, pk=None):
        """
        Triggers an asynchronous background Celery task to research and fetch competitors.
        """
        deal = self.get_object()
        instruction = str(request.data.get("instruction") or "").strip()
        existing_competitors = request.data.get("existing_competitors") or []
        if not isinstance(existing_competitors, list):
            existing_competitors = []

        if request.data.get("sync"):
            try:
                from .tasks import fetch_competitors_async_task
                result = fetch_competitors_async_task(
                    str(deal.id),
                    instruction=instruction,
                    existing_competitors=existing_competitors,
                )
                if result.get("error"):
                    return Response({"status": "FAILURE", "error": result["error"]}, status=400)
                from .services.competitor_intelligence import annotate_existing_competitors
                competitors = annotate_existing_competitors(deal, result.get("competitors", []))
                if not competitors:
                    return Response({
                        "status": "FAILURE",
                        "error": result.get("message", "Live web search returned no grounded competitors."),
                        "response": result.get("response", ""),
                        "competitors": [],
                        "diagnostics": result.get("diagnostics", {}),
                    })
                return Response({
                    "status": "SUCCESS",
                    "response": result.get("response", ""),
                    "competitors": competitors,
                    "message": result.get("message", "Competitor research completed."),
                    "diagnostics": result.get("diagnostics", {}),
                })
            except Exception as e:
                logger.error(f"Failed to run synchronous competitors search for deal {deal.id}: {str(e)}", exc_info=True)
                return Response({"error": f"Failed to run competitors research: {str(e)}"}, status=500)

        try:
            from .tasks import fetch_competitors_async_task
            task = fetch_competitors_async_task.apply_async(
                kwargs={
                    'deal_id': str(deal.id),
                    'instruction': instruction,
                    'existing_competitors': existing_competitors,
                },
                queue='high_priority'
            )
            return Response({
                "status": "queued",
                "task_id": task.id,
                "message": "Competitor research background task successfully initialized."
            })
        except Exception as e:
            logger.error(f"Failed to trigger async competitors task for deal {deal.id}: {str(e)}")
            return Response({"error": f"Failed to initialize competitors research: {str(e)}"}, status=500)

    @action(detail=True, methods=['get'], url_path='fetch_competitors_status/(?P<task_id>[^/.]+)')
    def fetch_competitors_status(self, request, pk=None, task_id=None):
        """
        Polls the execution status of the competitor research background task.
        """
        from celery.result import AsyncResult
        res = AsyncResult(task_id)
        if res.status == 'SUCCESS':
            data = res.result or {}
            if "error" in data:
                return Response({"status": "FAILURE", "error": data["error"]}, status=200)
            from .services.competitor_intelligence import annotate_existing_competitors
            competitors = annotate_existing_competitors(self.get_object(), data.get("competitors", []))
            if not competitors:
                return Response({
                    "status": "FAILURE",
                    "error": data.get("message", "Live web search returned no grounded competitors."),
                    "response": data.get("response", ""),
                    "competitors": [],
                    "diagnostics": data.get("diagnostics", {}),
                })
            return Response({
                "status": "SUCCESS",
                "response": data.get("response", ""),
                "competitors": competitors,
                "message": data.get("message", "Competitor research completed."),
                "diagnostics": data.get("diagnostics", {}),
            })
        elif res.status == 'FAILURE':
            return Response({
                "status": "FAILURE",
                "error": str(res.info or "Background execution failed unexpectedly.")
            }, status=500)
        
        return Response({
            "status": res.status,  # PENDING, STARTED, RETRY
        })

    @action(detail=True, methods=['post'])
    def fetch_company_news(self, request, pk=None):
        """
        Queues Claude web-search public-domain company news research and persists
        each completed run as a dated memo document.
        """
        deal = self.get_object()

        instruction = request.data.get("instruction", "")
        existing_news = request.data.get("existing_news", [])

        if request.data.get("sync"):
            try:
                from .tasks import fetch_company_news_async_task
                result = fetch_company_news_async_task(
                    deal_id=str(deal.id),
                    instruction=instruction,
                    existing_news=existing_news
                )
                if result.get("error"):
                    return Response({"status": "FAILURE", "error": result["error"]}, status=500)
                return Response({
                    "status": "SUCCESS",
                    **result,
                    "message": "Company news research completed and saved.",
                })
            except Exception as e:
                logger.error(f"Failed to run synchronous company news research for deal {deal.id}: {str(e)}", exc_info=True)
                return Response({"error": f"Failed to run company news research: {str(e)}"}, status=500)

        try:
            from .tasks import fetch_company_news_async_task
            task = fetch_company_news_async_task.apply_async(
                kwargs={
                    "deal_id": str(deal.id),
                    "instruction": instruction,
                    "existing_news": existing_news,
                },
                queue='high_priority',
            )
            return Response({
                "status": "queued",
                "task_id": task.id,
                "message": "Company news research background task successfully initialized.",
            })
        except Exception as e:
            logger.error(f"Failed to trigger async company news task for deal {deal.id}: {str(e)}", exc_info=True)
            return Response({"error": f"Failed to initialize company news research: {str(e)}"}, status=500)

    @action(detail=True, methods=['get'], url_path='fetch_company_news_status/(?P<task_id>[^/.]+)')
    def fetch_company_news_status(self, request, pk=None, task_id=None):
        """
        Polls the execution status of the company news research background task.
        """
        from celery.result import AsyncResult
        res = AsyncResult(task_id)
        if res.status == 'SUCCESS':
            data = res.result or {}
            if "error" in data:
                return Response({"status": "FAILURE", "error": data["error"]}, status=200)
            return Response({
                "status": "SUCCESS",
                **data,
            })
        elif res.status == 'FAILURE':
            return Response({
                "status": "FAILURE",
                "error": str(res.info or "Background company news research failed unexpectedly."),
            }, status=500)

        return Response({"status": res.status})

    @action(detail=True, methods=['post'])
    def save_competitors(self, request, pk=None):
        """
        Queues selected competitors for the ordered CIN -> VI -> embedding pipeline.
        Competitor research is stored as competitor VI relations, not as deal documents.
        """
        deal = self.get_object()
        competitors_text = request.data.get("competitors_text")
        if "competitors" in request.data and not request.data.get("competitors"):
            return Response({"error": "Select at least one competitor to save."}, status=400)

        competitors = []
        try:
            from .services.competitor_intelligence import (
                competitor_names_from_payload,
                competitor_names_from_text,
            )

            requested_competitors = request.data.get("competitors")
            requested_competitor_name = request.data.get("competitor_name")
            if requested_competitor_name:
                competitors = [{"name": requested_competitor_name, "notes": ""}]
            elif requested_competitors:
                competitors = competitor_names_from_payload(requested_competitors, limit=10)
            else:
                competitors = competitor_names_from_text(competitors_text, limit=10)

            if not competitors:
                return Response({"error": "Select at least one competitor to save."}, status=400)
        except Exception as vi_err:
            logger.warning(f"Failed to parse competitor VI enrichment request: {str(vi_err)}")
            return Response({"error": f"Failed to parse selected competitors: {str(vi_err)}"}, status=400)

        from .tasks import enrich_competitors_vi_async_task
        task = enrich_competitors_vi_async_task.apply_async(
            kwargs={
                "deal_id": str(deal.id),
                "competitors": competitors,
                "limit": 10,
            },
            queue='high_priority'
        )

        return Response({
            "status": "queued",
            "task_id": task.id,
            "message": "Selected competitors queued: private companies use CIN/VI, listed public companies use Screener, then profiles are embedded.",
        })

    @action(detail=True, methods=['get'], url_path='competitor_vi_status/(?P<task_id>[^/.]+)')
    def competitor_vi_status(self, request, pk=None, task_id=None):
        """
        Polls the execution status of the competitor VI enrichment background task.
        """
        from celery.result import AsyncResult
        res = AsyncResult(task_id)
        if res.status == 'SUCCESS':
            data = res.result or {}
            if data.get("status") == "FAILURE" or "error" in data:
                return Response({
                    "status": "FAILURE",
                    "error": data.get("error", "Competitor VI enrichment failed."),
                }, status=200)
            return Response({
                "status": "SUCCESS",
                "vi_enrichment": data.get("vi_enrichment") or {"enriched": [], "failed": [], "skipped": []},
                "enrichment": data.get("enrichment") or data.get("vi_enrichment") or {"enriched": [], "failed": [], "skipped": []},
            })
        if res.status == 'FAILURE':
            return Response({
                "status": "FAILURE",
                "error": str(res.info or "Background competitor VI enrichment failed unexpectedly."),
            }, status=500)

        return Response({"status": res.status})



from rest_framework.views import APIView
from deals.services.venture_intelligence import VentureIntelligenceService
from deals.serializers import VentureIntelligenceCompanyProfileSerializer
from deals.models import VentureIntelligenceCompanyProfile

class VentureIntelligenceResolveCinView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        company_name = request.data.get("company_name")
        cin = request.data.get("cin")

        if not company_name and not cin:
            return Response({"error": "Either company_name or cin is required"}, status=400)

        vi_service = VentureIntelligenceService()
        resolution = vi_service.resolve_company_identity(company_name=company_name, cin=cin)
        status_code = 200 if resolution.get("is_valid") else 404
        return Response({
            "success": bool(resolution.get("is_valid")),
            "cin": resolution.get("cin"),
            "entity_name": resolution.get("entity_name"),
            "confidence": resolution.get("confidence"),
            "source": resolution.get("source"),
            "cin_candidates": serialize_vi_cin_candidates(resolution),
            "message": "Resolved CIN successfully." if resolution.get("is_valid") else resolution.get("error") or "Could not resolve a valid CIN.",
        }, status=status_code)


class VentureIntelligencePreviewView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        company_name = request.data.get("company_name")
        cin = request.data.get("cin")
        
        if not company_name and not cin:
            return Response({"error": "Either company_name or cin is required"}, status=400)
            
        try:
            vi_service = VentureIntelligenceService()
            data, resolution = vi_service.fetch_resolved_company_details(company_name=company_name, cin=cin)
            data["resolved_cin"] = resolution.get("cin") or cin
            data["resolved_name"] = resolution.get("entity_name") or company_name
            data["resolution"] = {
                "cin": resolution.get("cin"),
                "entity_name": resolution.get("entity_name"),
                "confidence": resolution.get("confidence"),
                "source": resolution.get("source"),
                "is_valid": resolution.get("is_valid"),
                "cin_candidates": serialize_vi_cin_candidates(resolution),
                "used_cin": resolution.get("cin"),
            }
            return Response(data)
        except ValueError as e:
            return Response({"success": False, "message": str(e), "error": str(e)}, status=404)
        except Exception as e:
            return Response({"error": f"Failed to fetch from Venture Intelligence: {str(e)}"}, status=500)


class DealEnrichView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        try:
            deal = Deal.objects.get(id=pk)
        except Deal.DoesNotExist:
            return Response({"error": "Deal not found"}, status=404)

        company_name = request.data.get("company_name")
        cin = request.data.get("cin")
        relation_type = request.data.get("relation_type", "target")
        raw_data = request.data.get("raw_data")
        
        if relation_type not in ["target", "competitor"]:
            return Response({"error": "Invalid relation_type. Must be 'target' or 'competitor'"}, status=400)

        if not company_name and not cin:
            company_name = deal.title  # Fallback to deal title if empty

        if request.data.get("async"):
            from .tasks import enrich_deal_vi_async_task
            audit_log = AIRuntimeService.create_audit_log(
                source_type="deal_enrichment",
                source_id=str(deal.id),
                context_label=f"Venture Intelligence: {deal.title}",
                skill=AIRuntimeService.get_skill("venture_intelligence_enrichment"),
                status="PENDING",
                is_success=False,
                system_prompt="Queued for Venture Intelligence enrichment.",
                user_prompt=(
                    f"Enrich {relation_type} company "
                    f"{company_name or cin or deal.title} for deal {deal.title}."
                ),
                source_metadata={
                    "deal_id": str(deal.id),
                    "deal_title": deal.title,
                    "relation_type": relation_type,
                    "company_name": company_name,
                    "cin": cin,
                    "result_route": f"/deals/{deal.id}?tab=key-financials#venture-intelligence-section",
                },
            )
            task = enrich_deal_vi_async_task.apply_async(
                kwargs={
                    "deal_id": str(deal.id),
                    "company_name": company_name,
                    "cin": cin,
                    "relation_type": relation_type,
                    "raw_data": raw_data,
                    "audit_log_id": str(audit_log.id),
                },
                queue='high_priority'
            )
            audit_log.celery_task_id = task.id
            audit_log.save(update_fields=["celery_task_id"])
            return Response({
                "status": "queued",
                "task_id": task.id,
                "audit_log_id": str(audit_log.id),
                "message": f"Queued {relation_type} Venture Intelligence enrichment.",
            })

        vi_service = VentureIntelligenceService()
        try:
            profile = vi_service.enrich_deal(
                deal_id=deal.id,
                company_name=company_name,
                cin=cin,
                relation_type=relation_type,
                raw_data=raw_data,
            )
            serializer = VentureIntelligenceCompanyProfileSerializer(profile)
            return Response({
                "status": "success",
                "message": f"Successfully enriched deal with {relation_type} company profile.",
                "profile": serializer.data
            })
        except ValueError as e:
            logger.info(f"VI data unavailable for Deal {deal.id}: {e}")
            return Response({"error": str(e), "message": str(e)}, status=404)
        except Exception as e:
            logger.error(f"Enrichment failed for Deal {deal.id}: {e}", exc_info=True)
            return Response({"error": f"Enrichment failed: {str(e)}"}, status=500)


class DealEnrichStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk, task_id):
        from celery.result import AsyncResult

        result = AsyncResult(task_id)
        audit_log = AIAuditLog.objects.filter(celery_task_id=task_id).only("id").first()
        audit_log_id = str(audit_log.id) if audit_log else None
        if result.status == 'SUCCESS':
            data = result.result or {}
            if data.get("status") == "FAILURE" or "error" in data:
                return Response({
                    "status": "FAILURE",
                    "error": data.get("error", "VI enrichment failed."),
                    "audit_log_id": data.get("audit_log_id") or audit_log_id,
                }, status=200)

            profile = VentureIntelligenceCompanyProfile.objects.filter(id=data.get("profile_id")).first()
            return Response({
                "status": "SUCCESS",
                "profile": VentureIntelligenceCompanyProfileSerializer(profile).data if profile else None,
                "audit_log_id": data.get("audit_log_id") or audit_log_id,
            })

        if result.status == 'FAILURE':
            return Response({
                "status": "FAILURE",
                "error": str(result.info or "Background VI enrichment failed unexpectedly."),
                "audit_log_id": audit_log_id,
            }, status=500)

        return Response({"status": result.status, "audit_log_id": audit_log_id})
