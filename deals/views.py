import logging
from rest_framework import viewsets, filters, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema, extend_schema_view
from core.mixins import ErrorHandlingMixin
from .models import Deal
from .serializers import DealSerializer, DealListSerializer

logger = logging.getLogger(__name__)


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
