import logging
from rest_framework import viewsets, filters, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema, extend_schema_view
from core.mixins import ErrorHandlingMixin
from .models import Deal, DealDocument, DealPhaseLog
from .serializers import (
    DealSerializer, DealListSerializer, DealDetailSerializer, 
    DealDocumentSerializer, DealPhaseLogSerializer
)

logger = logging.getLogger(__name__)


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
    search_fields = ['title', 'extracted_text']
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
            
            # 1. Fetch relevant documents
            docs = DealDocument.objects.filter(is_indexed=True)
            if deal_id:
                docs = docs.filter(deal_id=deal_id)
                
            if not docs.exists():
                return Response({"response": "No indexed documents found to search through."}, status=200)
                
            context = "\n\n".join([f"--- DOC: {d.title} (Deal: {d.deal.title}) ---\n{d.extracted_text[:2000]}..." for d in docs])

            # 2. Use AI for search
            ai_service = AIProcessorService()
            prompt = f"Using the following institutional documents as context, answer: {query}\n\nCONTEXT:\n{context}"
            
            result = ai_service.process_content(
                content=prompt,
                skill_name="deal_extraction",
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
    queryset = Deal.objects.select_related('bank', 'primary_contact', 'request').all()
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['title', 'deal_summary', 'industry', 'sector', 'city', 'state', 'country']
    ordering_fields = ['created_at', 'title', 'priority']
    ordering = ['-created_at']
    filterset_fields = ['bank', 'priority', 'fund', 'is_female_led', 'management_meeting']
    
    def get_serializer_class(self):
        # Use lightweight serializer for list views to reduce payload size
        if self.action == 'list':
            return DealListSerializer
        if self.action == 'retrieve':
            return DealDetailSerializer
        return DealSerializer
    
    def perform_create(self, serializer):
        # source_email_id, contact_discovery, and analysis_json are passed in validated_data
        source_email_id = serializer.validated_data.pop('source_email_id', None)
        contact_discovery = serializer.validated_data.pop('contact_discovery', None)
        analysis_json = serializer.validated_data.pop('analysis_json', None)
        
        # If strings, parse to dict
        import json
        if isinstance(contact_discovery, str):
            try: contact_discovery = json.loads(contact_discovery)
            except: contact_discovery = None
        if isinstance(analysis_json, str):
            try: analysis_json = json.loads(analysis_json)
            except: analysis_json = None
                
        deal = serializer.save()
        
        # 0. Handle Ambiguities mapping from AI metadata
        if analysis_json and 'metadata' in analysis_json:
            try:
                ambiguities = analysis_json['metadata'].get('ambiguous_points', [])
                if ambiguities:
                    deal.ambiguities = ambiguities
                    deal.save(update_fields=['ambiguities'])
            except: pass

        # 1. Handle Contact & Bank Discovery
        if contact_discovery:
            try:
                from banks.models import Bank
                from contacts.models import Contact
                
                firm_name = contact_discovery.get('firm_name')
                firm_domain = contact_discovery.get('firm_domain')
                banker_name = contact_discovery.get('name')
                
                bank = None
                if firm_domain:
                    bank = Bank.objects.filter(website_domain__iexact=firm_domain).first()
                if not bank and firm_name:
                    bank = Bank.objects.filter(name__icontains=firm_name).first()
                
                # Create Bank if not found
                if not bank and firm_name:
                    bank = Bank.objects.create(name=firm_name, website_domain=firm_domain)
                
                if banker_name:
                    # Find or create contact
                    contact, created = Contact.objects.get_or_create(
                        name=banker_name,
                        bank=bank,
                        defaults={
                            'designation': contact_discovery.get('designation'),
                            'linkedin_url': contact_discovery.get('linkedin')
                        }
                    )
                    deal.primary_contact = contact
                    if bank: deal.bank = bank
                    
                    # Increment source count for influencer tracking
                    contact.source_count += 1
                    contact.save(update_fields=['source_count'])
                    deal.save(update_fields=['primary_contact', 'bank'])
                    print(f"[DISCOVERY] Linked {deal.title} to {banker_name} ({firm_name})")
            except Exception as e:
                logger.error(f"Discovery error: {str(e)}")

        # 2. Handle Email Linking & Threading
        if source_email_id:
            try:
                from microsoft.models import Email
                from ai_orchestrator.services.embedding_processor import EmbeddingService
                
                source_email = Email.objects.filter(id=source_email_id).first()
                if source_email:
                    source_email.deal = deal
                    source_email.is_processed = True
                    source_email.save(update_fields=['deal', 'is_processed'])
                    
                    # LINK THE WHOLE THREAD (All replies/forwards in this conversation)
                    if source_email.conversation_id:
                        Email.objects.filter(
                            conversation_id=source_email.conversation_id
                        ).update(deal=deal)
                        print(f"[THREADING] Linked entire thread {source_email.conversation_id} to deal")

                    # 3. Create DealDocument records for attachments
                    if source_email.attachments:
                        from .models import DealDocument, DocumentType
                        for att in source_email.attachments:
                            # Avoid duplicates
                            if not DealDocument.objects.filter(deal=deal, title=att.get('name')).exists():
                                # Determine type from filename
                                name = att.get('name', '').lower()
                                doc_type = DocumentType.OTHER
                                if 'financial' in name or 'mis' in name or 'model' in name: doc_type = DocumentType.FINANCIALS
                                elif 'legal' in name or 'sha' in name or 'ssa' in name: doc_type = DocumentType.LEGAL
                                elif 'teaser' in name or 'deck' in name or 'pitch' in name: doc_type = DocumentType.PITCH_DECK
                                
                                DealDocument.objects.create(
                                    deal=deal,
                                    title=att.get('name'),
                                    document_type=doc_type,
                                    onedrive_id=att.get('id'),
                                    uploaded_by=request.user.profile if hasattr(request.user, 'profile') else None
                                )
                                print(f"[DOCUMENT] Created DealDocument artifact: {att.get('name')}")
                                
                                # 4. Semantic Indexing for the Document
                                try:
                                    # Since we don't have the text yet (it needs download/OCR),
                                    # the document processor will usually handle this later.
                                    # BUT, if we want to trigger it, we need text.
                                    # For now, we've registered the intent.
                                    pass
                                except Exception as e:
                                    logger.error(f"Doc indexing failed: {str(e)}")

                    # Copy extracted text to deal if empty
                    if not deal.extracted_text and source_email.extracted_text:
                        deal.extracted_text = source_email.extracted_text
                        deal.save(update_fields=['extracted_text'])
                    
                    # Asynchronous vectorization
                    try:
                        embed_service = EmbeddingService()
                        embed_service.vectorize_deal(deal)
                        embed_service.vectorize_email(source_email)
                    except Exception as e:
                        logger.error(f"Vectorization failed: {str(e)}")
            except Exception as e:
                logger.error(f"Email linking failed: {str(e)}")

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
        to_phase = request.data.get('to_phase')
        rationale = request.data.get('rationale')
        
        if not to_phase:
            return Response({"error": "to_phase is required"}, status=400)
            
        from_phase = getattr(deal, 'current_phase', None)
        
        # 1. Update the Deal
        deal.current_phase = to_phase
        deal.save(update_fields=['current_phase'])
        
        # 2. Log the transition
        DealPhaseLog.objects.create(
            deal=deal,
            from_phase=from_phase,
            to_phase=to_phase,
            rationale=rationale,
            changed_by=request.user.profile if hasattr(request.user, 'profile') else None
        )
        
        return Response({
            "status": "success",
            "from_phase": from_phase,
            "to_phase": to_phase
        })

    @action(detail=True, methods=['post'])
    def update_flow_state(self, request, pk=None):
        """
        Unified endpoint for the 18-stage interactive deal flow.
        Accepts `active_stage`, `decisions_update` (dict), and optional `reason`.
        """
        deal = self.get_object()
        active_stage = request.data.get('active_stage')
        decisions_update = request.data.get('decisions_update')
        reason = request.data.get('reason')
        
        # 1. Update Decisions (Allow reset if explicitly empty dict)
        if decisions_update is not None:
            if decisions_update == {}:
                deal.deal_flow_decisions = {}
            else:
                current_decisions = deal.deal_flow_decisions or {}
                current_decisions.update(decisions_update)
                deal.deal_flow_decisions = current_decisions
        
        # 2. Update active stage & create log if changed
        if active_stage and deal.current_phase != active_stage:
            from_phase = deal.current_phase
            deal.current_phase = active_stage
            
            DealPhaseLog.objects.create(
                deal=deal,
                from_phase=from_phase,
                to_phase=active_stage,
                rationale=reason,
                changed_by=request.user.profile if hasattr(request.user, 'profile') else None
            )
            
        # 3. Rejection tracking (Check for presence in request.data to allow nulling)
        if 'rejection_stage_id' in request.data:
            deal.rejection_stage_id = request.data.get('rejection_stage_id')
            deal.rejection_reason = reason
            
        deal.save(update_fields=['deal_flow_decisions', 'current_phase', 'rejection_stage_id', 'rejection_reason'])
        
        return Response({
            "status": "success",
            "current_phase": deal.current_phase,
            "deal_flow_decisions": deal.deal_flow_decisions
        })

    @action(detail=False, methods=['post'])
    def analyze_folder(self, request):
        """
        Kicks off an asynchronous folder analysis. Returns a task_id.
        """
        import traceback
        try:
            folder_id = request.data.get('folder_id')
            folder_name = request.data.get('folder_name', folder_id)
            drive_id = request.data.get('drive_id')
            
            if not folder_id:
                return Response({"error": "folder_id is required"}, status=400)
                
            from microsoft.services.graph_service import DMS_USER_EMAIL
            from .tasks import analyze_folder_async
            from ai_orchestrator.models import AIAuditLog, AIPersonality, AISkill
            
            # 1. Create a PENDING audit log immediately for visibility
            personality = AIPersonality.objects.filter(is_default=True).first()
            skill = AISkill.objects.filter(name='deal_extraction').first()
            
            audit_log = AIAuditLog.objects.create(
                source_type='onedrive_folder',
                source_id=folder_id,
                context_label=f"Folder: {folder_name}",
                personality=personality,
                skill=skill,
                status='PENDING',
                is_success=False,
                model_used='qwen3.5:latest',
                system_prompt="Queued for forensic traversal...",
                user_prompt=f"Queued analysis for folder: {folder_name}"
            )

            # 2. Trigger task
            task = analyze_folder_async.apply_async(
                kwargs={
                    'drive_id': drive_id,
                    'folder_id': folder_id,
                    'user_email': DMS_USER_EMAIL,
                    'audit_log_id': str(audit_log.id) 
                },
                queue='celery'
            )
            
            audit_log.celery_task_id = task.id
            audit_log.save()
            
            return Response({
                "task_id": task.id,
                "audit_log_id": str(audit_log.id),
                "status": "queued"
            })
        except Exception as e:
            print(f"[CRITICAL ERROR] analyze_folder failed:")
            print(traceback.format_exc())
            return Response({"error": str(e)}, status=500)

    @action(detail=False, methods=['get'], url_path='task-status/(?P<task_id>[^/.]+)')
    def task_status(self, request, task_id=None):
        """
        Polls the status of an AI analysis task.
        """
        from celery.result import AsyncResult
        from django.core.cache import cache
        import uuid
        
        result = AsyncResult(task_id)
        
        response = {
            "task_id": task_id,
            "status": result.status, # PENDING, STARTED, SUCCESS, FAILURE
        }
        
        if result.status == 'SUCCESS':
            data = result.result
            if "error" in data:
                return Response(data, status=500)
                
            # If successful, we wrap it in a session ID for the confirmation step
            # exactly like before, but now we get the data from the task result
            session_id = str(uuid.uuid4())
            cache.set(f"folder_sync_{session_id}", {
                "file_tree": data['file_tree'],
                "drive_id": data['drive_id'],
                "folder_id": data['folder_id'],
                "user_email": data['user_email'],
                "preliminary_data": data['preliminary_data'],
                "preview_text": data.get('preview_text', '')
            }, timeout=3600)
            
            response.update({
                "session_id": session_id,
                "folder_id": data['folder_id'],
                "total_files": data['total_files'],
                "preview_files_analyzed": data['preview_files_analyzed'],
                "preliminary_data": data['preliminary_data'],
                "raw_thinking": data.get('raw_thinking', '')
            })
            
        elif result.status == 'FAILURE':
            response["error"] = str(result.info)
            
        return Response(response)

    @action(detail=False, methods=['post'])
    def create_from_audit_log(self, request):
        """
        Kicks off the confirmation step for an existing audit log.
        Re-caches the session data so it can be confirmed via standard confirm_folder_deal.
        """
        log_id = request.data.get('audit_log_id')
        if not log_id:
            return Response({"error": "audit_log_id is required"}, status=400)
            
        from ai_orchestrator.models import AIAuditLog
        from django.core.cache import cache
        import uuid
        
        try:
            log = AIAuditLog.objects.get(id=log_id)
            if log.source_type != 'onedrive_folder':
                return Response({"error": "This audit log is not a folder analysis"}, status=400)
            
            meta = log.source_metadata
            if not meta:
                return Response({"error": "This log does not contain source metadata"}, status=400)
                
            # Re-cache exactly like the task_status poller does
            from microsoft.services.graph_service import DMS_USER_EMAIL
            session_id = str(uuid.uuid4())
            cache.set(f"folder_sync_{session_id}", {
                "file_tree": meta['file_tree'],
                "drive_id": meta['drive_id'],
                "folder_id": meta['folder_id'],
                "user_email": DMS_USER_EMAIL,
                "preliminary_data": log.parsed_json,
                "preview_text": meta.get('preview_text', '')
            }, timeout=3600)
            
            return Response({
                "session_id": session_id,
                "preliminary_data": log.parsed_json,
                "total_files": meta.get('total_files', len(meta['file_tree'])),
                "preview_files_analyzed": 5, # Default
                "raw_thinking": log.raw_response # Fallback
            })
            
        except AIAuditLog.DoesNotExist:
            return Response({"error": "Audit log not found"}, status=404)

    @action(detail=False, methods=['post'])
    def confirm_folder_deal(self, request):
        """
        Creates the Deal from the preliminary analysis and kicks off the background
        indexing task for the rest of the folder.
        """
        session_id = request.data.get('session_id')
        deal_data = request.data.get('deal_data', {})
        
        if not session_id or not deal_data:
            return Response({"error": "session_id and deal_data are required"}, status=400)
            
        from django.core.cache import cache
        session_data = cache.get(f"folder_sync_{session_id}")
        
        if not session_data:
            return Response({"error": "Session expired or invalid. Please re-analyze the folder."}, status=400)
            
        # 1. Create the Deal
        serializer = self.get_serializer(data=deal_data)
        if serializer.is_valid():
            # Extract forensic mapping from session data if not in deal_data
            # Just like perform_create does for emails
            analysis_json = session_data.get('preliminary_data', {})
            
            deal = serializer.save(
                processing_status='processing',
                source_onedrive_id=session_data.get('folder_id'),
                extracted_text=session_data.get('preview_text', '')
            )
            
            # Map forensic fields manually if they came from AI
            if analysis_json:
                deal.analysis_json = analysis_json
                if 'metadata' in analysis_json:
                    deal.ambiguities = analysis_json['metadata'].get('ambiguous_points', [])
                if 'deal_model_data' in analysis_json:
                    deal.themes = analysis_json['deal_model_data'].get('themes', [])
                
                # IMPORTANT: Populate the initial Source Data Hub with the preview text
                # so it's not empty while background indexing runs.
                if 'analyst_report' in analysis_json:
                    # We can use the report as initial text or if we have raw combined text
                    # In our case, we'll store the report itself as part of the summary,
                    # but extracted_text should ideally contain the raw signals.
                    # For now, let's keep it clean.
                    pass
                deal.save()
            
            # 2. Trigger Background Task
            from .tasks import process_deal_folder_background
            process_deal_folder_background.apply_async(
                kwargs={
                    'deal_id': str(deal.id),
                    'file_tree_map': session_data['file_tree'],
                    'user_email': session_data['user_email']
                },
                queue='celery'
            )
            
            # Optionally clear cache
            cache.delete(f"folder_sync_{session_id}")
            
            return Response({
                "status": "success",
                "deal_id": deal.id,
                "message": f"Deal created. Processing {len(session_data['file_tree'])} files in background."
            }, status=201)
            
        return Response(serializer.errors, status=400)

    @action(detail=False, methods=['get'], url_path='task-status/(?P<task_id>[^/.]+)')
    def task_status(self, request, task_id=None):
        """
        Polls the status of an AI analysis task.
        """
        from celery.result import AsyncResult
        from django.core.cache import cache
        import uuid
        
        result = AsyncResult(task_id)
        
        response = {
            "task_id": task_id,
            "status": result.status, # PENDING, STARTED, SUCCESS, FAILURE
        }
        
        if result.status == 'SUCCESS':
            data = result.result
            if not data:
                 return Response({"status": "FAILURE", "error": "Task returned no data"}, status=500)
            if "error" in data:
                return Response(data, status=500)
                
            # If successful, we wrap it in a session ID for the confirmation step
            session_id = str(uuid.uuid4())
            cache.set(f"folder_sync_{session_id}", {
                "file_tree": data['file_tree'],
                "drive_id": data['drive_id'],
                "folder_id": data['folder_id'],
                "user_email": data['user_email'],
                "preliminary_data": data['preliminary_data'],
                "preview_text": data.get('preview_text', '')
            }, timeout=3600)
            
            response.update({
                "session_id": session_id,
                "folder_id": data['folder_id'],
                "total_files": data['total_files'],
                "preview_files_analyzed": data['preview_files_analyzed'],
                "preliminary_data": data['preliminary_data'],
                "raw_thinking": data.get('raw_thinking', '')
            })
            
        elif result.status == 'FAILURE':
            response["error"] = str(result.info)
            
        return Response(response)

    @action(detail=True, methods=['post'])
    def analyze_additional_documents(self, request, pk=None):
        """
        Updates the AI Summary (V2 Analysis) using the existing analysis and newly selected documents.
        """
        deal = self.get_object()
        document_ids = request.data.get('document_ids', [])
        
        if not document_ids:
            return Response({"error": "No document IDs provided"}, status=400)
            
        docs = deal.documents.filter(id__in=document_ids)
        if not docs.exists():
            return Response({"error": "No matching documents found for this deal"}, status=400)
            
        # 1. Combine new text
        new_text_context = ""
        for doc in docs:
            if doc.extracted_text:
                new_text_context += f"\n\n--- NEW DOCUMENT: {doc.title} ---\n{doc.extracted_text}"
                
        if not new_text_context.strip():
            return Response({"error": "Selected documents have no extracted text to analyze"}, status=400)

        # 2. Build Prompt Context
        from ai_orchestrator.services.ai_processor import AIProcessorService
        from ai_orchestrator.models import AIAuditLog, AIPersonality, AISkill
        import json
        from django.utils import timezone
        
        ai_service = AIProcessorService()
        personality = AIPersonality.objects.filter(is_default=True).first()
        
        existing_summary = deal.deal_summary or ""
        existing_json = json.dumps(deal.analysis_json, default=str) if deal.analysis_json else "{}"
        
        # We now use the database skill instead of hardcoding the prompt
        # We pass the context via metadata to process_content
        current_version = len(deal.analysis_history or []) + 2 # V1 is deal_summary, so next is V2
        
        # 3. Create Audit Log
        audit_log = AIAuditLog.objects.create(
            source_type='vdr_incremental_analysis',
            source_id=str(deal.id),
            personality=personality,
            status='PROCESSING',
            is_success=False,
            model_used='qwen3.5:latest',
            system_prompt="Updating existing deal analysis with new documents.",
            user_prompt=new_text_context
        )
        
        # 4. Run Analysis
        try:
            result = ai_service.process_content(
                content=new_text_context,
                skill_name="vdr_incremental_analysis",
                source_type="vdr_incremental_analysis",
                source_id=str(deal.id),
                metadata={
                    'audit_log_id': str(audit_log.id),
                    'existing_summary': existing_summary,
                    'version_num': current_version
                }
            )
            
            analysis = {}
            raw_thinking = ""
            if isinstance(result, dict) and 'parsed_json' in result:
                analysis = result['parsed_json']
                raw_thinking = result.get('thinking', '')
            else:
                analysis = result
                raw_thinking = analysis.get('thinking', '') if isinstance(analysis, dict) else ""
                
            if analysis and "error" not in analysis:
                # Update Deal JSON Fields
                deal.analysis_json = analysis
                if 'deal_model_data' in analysis:
                    deal.themes = analysis['deal_model_data'].get('themes', deal.themes)
                if 'metadata' in analysis:
                    deal.ambiguities = analysis['metadata'].get('ambiguous_points', deal.ambiguities)
                    
                # Append to History instead of overwriting V1
                if 'analyst_report' in analysis:
                    new_history = list(deal.analysis_history or [])
                    new_history.append({
                        "version": current_version,
                        "report": analysis['analyst_report'],
                        "timestamp": timezone.now().isoformat(),
                        "documents_analyzed": [d.title for d in docs]
                    })
                    deal.analysis_history = new_history
                
                # Append thinking
                if raw_thinking:
                    deal.thinking = (deal.thinking or "") + f"\n\n--- V{current_version} INCREMENTAL ANALYSIS ---\n{raw_thinking}"
                    
                deal.save()
                
                # Mark docs as analyzed
                docs.update(is_ai_analyzed=True)
                
                return Response({
                    "status": "success",
                    "message": f"Successfully updated analysis using {docs.count()} documents."
                })
            else:
                 return Response({"error": "AI failed to generate a valid updated summary.", "details": analysis}, status=500)
                 
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Incremental analysis failed: {str(e)}")
            return Response({"error": str(e)}, status=500)

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
            
            doc_processor = DocumentProcessorService()
            embed_service = EmbeddingService()
            
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
                
            extracted_text = doc_processor.extract_text(file_content, file_name)
            
            from .models import DealDocument
            doc = DealDocument.objects.create(
                deal=deal,
                title=file_name,
                document_type=doc_type,
                extracted_text=extracted_text,
                is_indexed=False,
                is_ai_analyzed=False,
                uploaded_by=request.user.profile if hasattr(request.user, 'profile') else None
            )
            
            if extracted_text and len(extracted_text.strip()) > 50:
                new_context = f"\n\n--- MANUAL DOCUMENT: {file_name} ---\n{extracted_text}"
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
