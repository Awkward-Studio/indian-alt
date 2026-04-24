import logging
from decimal import Decimal, InvalidOperation

import django_filters.rest_framework as django_filters
from rest_framework import viewsets, filters, status
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema, extend_schema_view
from core.mixins import ErrorHandlingMixin
from .models import Deal, DealDocument, DealPhaseLog
from .serializers import (
    DealSerializer, DealListSerializer, DealDetailSerializer, 
    DealDocumentSerializer, DealPhaseLogSerializer, DealHeavyFieldsSerializer
)
from .services.deal_creation import DealCreationService
from .services.document_artifacts import DocumentArtifactService
from .services.deal_flow import DealFlowService
from .services.folder_analysis import FolderAnalysisService
from ai_orchestrator.services.runtime import AIRuntimeService

logger = logging.getLogger(__name__)


class DealPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


class DealFilterSet(django_filters.FilterSet):
    sector = django_filters.CharFilter(lookup_expr='icontains')
    city = django_filters.CharFilter(lookup_expr='icontains')

    class Meta:
        model = Deal
        fields = [
            'bank', 'priority', 'deal_status', 'fund', 'is_female_led',
            'management_meeting', 'business_proposal_stage', 'ic_stage',
            'current_phase', 'sector', 'city', 'primary_contact'
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
            prompt = f"Using the following institutional documents as context, answer: {query}\n\nCONTEXT:\n{context}"
            
            result = ai_service.process_content(
                content=prompt,
                skill_name=None,
                source_type="global_search"
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
    queryset = Deal.objects.select_related('bank', 'primary_contact', 'request').prefetch_related('responsibility', 'additional_contacts').all()
    permission_classes = [IsAuthenticated]
    pagination_class = DealPagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = DealFilterSet
    search_fields = [
        'title', 'deal_summary', 'industry', 'sector', 'city', 'state',
        'country', 'fund', 'priority', 'deal_status', 'current_phase',
        'funding_ask_for', 'bank__name',
        'legacy_investment_bank', 'primary_contact__name'
    ]
    ordering_fields = [
        'created_at', 'title', 'priority', 'deal_status',
        'sector', 'industry', 'fund', 'current_phase', 'city'
    ]
    ordering = ['-created_at']
    @staticmethod
    def _parse_funding_ask(value):
        if value in (None, ''):
            return 0.0

        cleaned_value = str(value).strip().replace(',', '')
        try:
            return float(Decimal(cleaned_value))
        except (InvalidOperation, ValueError, TypeError):
            return 0.0

    @action(detail=False, methods=['get'])
    def dashboard_metrics(self, request):
        """
        Calculates aggregate metrics for the dashboard without loading all records.
        """
        queryset = Deal.objects.all()

        total_value = 0.0
        invested_ytd = 0.0
        for deal_status, funding_ask in queryset.values_list('deal_status', 'funding_ask').iterator(chunk_size=1000):
            parsed_amount = self._parse_funding_ask(funding_ask)
            total_value += parsed_amount
            if deal_status == 'Invested':
                invested_ytd += parsed_amount

        return Response({
            'totalDeals': queryset.count(),
            'activeDeals': queryset.exclude(deal_status__in=['Passed', 'Invested', 'Portfolio']).count(),
            'closedDeals': queryset.filter(deal_status__in=['Invested', 'Portfolio']).count(),
            'totalValue': total_value,
            'investedYTD': invested_ytd,
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
        result = DealFlowService.transition_phase(
            deal=deal,
            to_phase=request.data.get('to_phase'),
            rationale=request.data.get('rationale'),
            request_user=request.user
        )
        return Response(result)

    @action(detail=True, methods=['post'])
    def update_flow_state(self, request, pk=None):
        """
        Unified endpoint for the 18-stage interactive deal flow.
        Accepts `active_stage`, `decisions_update` (dict), and optional `reason`.
        """
        deal = self.get_object()
        result = DealFlowService.update_flow_state(
            deal=deal,
            active_stage=request.data.get('active_stage'),
            decisions_update=request.data.get('decisions_update'),
            reason=request.data.get('reason'),
            rejection_stage_id=request.data.get('rejection_stage_id'),
            request_user=request.user
        )
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
