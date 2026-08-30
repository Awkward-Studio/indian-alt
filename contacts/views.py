import re

from rest_framework import filters, status, viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from django.db.models import Count, DateField, F, Max, Q
from django.db.models.functions import Cast, Coalesce
from django_filters.rest_framework import DjangoFilterBackend
from django_filters import FilterSet, CharFilter
from drf_spectacular.utils import extend_schema_view, extend_schema
from core.mixins import ErrorHandlingMixin
from .models import Contact, ContactCardExtraction, WorkplaceVerificationSuggestion
from contacts.services.banker_analytics import (
    bank_analytics_queryset,
    banker_analytics_queryset,
    deal_activity_queryset,
)
from contacts.services.workplace_verification import (
    WorkplaceVerificationService,
    review_workplace_suggestion,
)
from .serializers import (
    BankAnalyticsSerializer,
    BankerAnalyticsSerializer,
    ContactSerializer,
    ContactListSerializer,
    ContactCardExtractionSerializer,
    WorkplaceVerificationReviewSerializer,
    WorkplaceVerificationSuggestionSerializer,
)


class ContactCardExtractionViewSet(viewsets.ModelViewSet):
    serializer_class = ContactCardExtractionSerializer
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    http_method_names = ['get', 'post', 'delete', 'head', 'options']

    def get_queryset(self):
        queryset = ContactCardExtraction.objects.select_related('matched_contact')
        return queryset if self.request.user.is_staff else queryset.filter(created_by=self.request.user)

    def create(self, request, *args, **kwargs):
        uploaded = request.FILES.get('file')
        if not uploaded:
            return Response({'detail': 'Choose a visiting-card image.'}, status=400)
        if uploaded.size > 10 * 1024 * 1024:
            return Response({'detail': 'The image must be 10 MB or smaller.'}, status=400)
        from ai_orchestrator.services.document_processor import DocumentProcessorService
        result = DocumentProcessorService().get_extraction_result(uploaded.read(), uploaded.name, page_limit=1, hint='Visiting card: extract contact details accurately.')
        text = str(result.get('normalized_text') or result.get('text') or '').strip()
        if not text:
            return Response({'detail': 'No readable contact details were found.'}, status=422)
        email_match = re.search(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', text)
        phone_match = re.search(r'(?:\+?\d[\d ()-]{7,}\d)', text)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        data = {
            'name': lines[0] if lines else '',
            'designation': lines[1] if len(lines) > 1 else '',
            'email': email_match.group(0) if email_match else '',
            'phone': phone_match.group(0).strip() if phone_match else '',
            'contact_type': 'OTHER',
        }
        match = Contact.objects.filter(email__iexact=data['email']).first() if data['email'] else None
        if not match and data['phone']:
            match = Contact.objects.filter(phone=data['phone']).first()
        extraction = ContactCardExtraction.objects.create(created_by=request.user, file_name=uploaded.name, file_size=uploaded.size, raw_text=text, extracted_data=data, matched_contact=match)
        return Response(self.get_serializer(extraction).data, status=201)

    @action(detail=True, methods=['post'])
    def review(self, request, pk=None):
        extraction = self.get_object()
        data = request.data.get('extracted_data') or extraction.extracted_data
        contact_id = request.data.get('contact_id') or extraction.matched_contact_id
        contact = Contact.objects.filter(id=contact_id).first() if contact_id else None
        allowed = {key: data.get(key) for key in ('name', 'email', 'phone', 'designation', 'contact_type') if key in data}
        if contact:
            for key, value in allowed.items():
                if value:
                    setattr(contact, key, value)
            contact.save()
        else:
            contact = Contact.objects.create(**allowed)
        extraction.extracted_data = data
        extraction.matched_contact = contact
        extraction.status = ContactCardExtraction.Status.COMPLETED
        extraction.save(update_fields=['extracted_data', 'matched_contact', 'status', 'updated_at'])
        return Response({'extraction': self.get_serializer(extraction).data, 'contact': ContactSerializer(contact).data})


class ContactFilterSet(FilterSet):
    """Custom filter set for Contact model to handle ArrayField."""
    sector_coverage = CharFilter(method='filter_sector_coverage')
    designation = CharFilter(method='filter_designation')
    location = CharFilter(field_name='location', lookup_expr='icontains')
    contact_type = CharFilter(field_name='contact_type', lookup_expr='iexact')

    def filter_sector_coverage(self, queryset, _name, value):
        return queryset.filter(sector_coverage__icontains=value.strip())

    def filter_designation(self, queryset, _name, value):
        normalized = value.strip().lower()
        if normalized == 'md':
            return queryset.filter(
                Q(designation__icontains='managing director')
                | Q(designation__iexact='md')
            )
        if normalized == 'director':
            return queryset.filter(
                designation__icontains='director'
            ).exclude(designation__icontains='managing director')
        if normalized == 'vp':
            return queryset.filter(
                Q(designation__icontains='vice president')
                | Q(designation__iexact='vp')
            )
        return queryset.filter(designation__icontains=value.strip())
    
    class Meta:
        model = Contact
        fields = ['bank', 'sector_coverage', 'designation', 'location', 'contact_type']


@extend_schema_view(
    list=extend_schema(
        summary="List all contacts",
        description="Retrieve a list of all contacts with optional filtering and search.",
        tags=["Contacts"],
    ),
    create=extend_schema(
        summary="Create a new contact",
        description="Create a new contact record.",
        tags=["Contacts"],
    ),
    retrieve=extend_schema(
        summary="Retrieve a contact",
        description="Get detailed information about a specific contact.",
        tags=["Contacts"],
    ),
    update=extend_schema(
        summary="Update a contact",
        description="Update all fields of a contact record.",
        tags=["Contacts"],
    ),
    partial_update=extend_schema(
        summary="Partially update a contact",
        description="Update specific fields of a contact record.",
        tags=["Contacts"],
    ),
    destroy=extend_schema(
        summary="Delete a contact",
        description="Delete a contact record.",
        tags=["Contacts"],
    ),
)
class ContactViewSet(ErrorHandlingMixin, viewsets.ModelViewSet):
    # select_related prevents N+1 queries when accessing bank.name
    queryset = (
        Contact.objects.select_related('bank')
        .prefetch_related('primary_deals', 'additional_deals')
        .annotate(
            deal_count=Count('primary_deals', distinct=True),
            last_deal_date=Max(Coalesce('primary_deals__received_at', Cast(F('primary_deals__created_at'), output_field=DateField()), output_field=DateField())),
        )
    )
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    search_fields = [
        'name', 'email', 'designation', 'contact_type', 'location', 'bank__name',
        'sector_coverage',
    ]
    ordering_fields = [
        'name', 'email', 'designation', 'contact_type', 'location', 'sector_coverage',
        'deal_count', 'created_at',
    ]
    ordering = ['-created_at']
    filterset_class = ContactFilterSet
    
    def get_serializer_class(self):
        # Use lightweight serializer for list to reduce response size
        if self.action == 'list':
            return ContactListSerializer
        return ContactSerializer

    def _can_review_workplace(self, request, contact):
        user = request.user
        if user.is_superuser or user.is_staff:
            return True
        profile = getattr(user, 'profile', None)
        if profile is None or profile.is_disabled:
            return False
        responsibilities = {
            str(value) for value in (contact.responsibility or [])
        }
        return profile.is_admin or str(profile.id) in responsibilities

    @action(detail=True, methods=['get', 'post'], url_path='workplace-verification')
    def workplace_verification(self, request, pk=None):
        contact = self.get_object()
        if request.method == 'GET':
            suggestions = contact.workplace_verifications.select_related(
                'requested_by', 'reviewed_by', 'audit_log',
            )[:20]
            return Response(
                WorkplaceVerificationSuggestionSerializer(
                    suggestions,
                    many=True,
                ).data
            )

        try:
            suggestion, audit = WorkplaceVerificationService().verify(
                contact=contact,
                requested_by=request.user,
            )
        except ValueError as exc:
            return Response(
                {'detail': str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if suggestion is None:
            return Response({
                'detail': 'No credible workplace suggestion was found.',
                'suggestion': None,
                'audit_log': str(audit.id),
            })
        return Response(
            WorkplaceVerificationSuggestionSerializer(suggestion).data,
            status=status.HTTP_201_CREATED,
        )

    @action(
        detail=True,
        methods=['post'],
        url_path=r'workplace-verification/(?P<suggestion_id>[^/.]+)/review',
    )
    def review_workplace_verification(
        self,
        request,
        pk=None,
        suggestion_id=None,
    ):
        contact = self.get_object()
        if not self._can_review_workplace(request, contact):
            return Response(
                {'detail': 'You do not have permission to review this suggestion.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        suggestion = WorkplaceVerificationSuggestion.objects.filter(
            id=suggestion_id,
            contact=contact,
        ).first()
        if suggestion is None:
            return Response(
                {'detail': 'Workplace suggestion not found.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        input_serializer = WorkplaceVerificationReviewSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        try:
            reviewed = review_workplace_suggestion(
                suggestion=suggestion,
                reviewer=request.user,
                **input_serializer.validated_data,
            )
        except ValueError as exc:
            return Response(
                {'detail': str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(WorkplaceVerificationSuggestionSerializer(reviewed).data)


@extend_schema(
    tags=["Contacts"],
    summary="Banker and bank sourcing analytics",
    description=(
        "Returns primary-sourcing deal volume, active mandates, conversion metrics, "
        "and last-deal dates. Use entity_type=banker or entity_type=bank. A detail "
        "request also returns the entity's recent deal activity."
    ),
)
class BankerAnalyticsViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated]

    def _entity_type(self, request):
        entity_type = request.query_params.get("entity_type", "banker").strip().lower()
        if entity_type not in {"banker", "bank"}:
            raise ValidationError(
                {"entity_type": "Must be either 'banker' or 'bank'."}
            )
        return entity_type

    def _activity_limit(self, request):
        raw_limit = request.query_params.get("activity_limit", "20")
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                {"activity_limit": "Must be an integer between 1 and 100."}
            ) from exc
        if not 1 <= limit <= 100:
            raise ValidationError(
                {"activity_limit": "Must be an integer between 1 and 100."}
            )
        return limit

    def list(self, request):
        entity_type = self._entity_type(request)
        queryset = (
            banker_analytics_queryset()
            if entity_type == "banker"
            else bank_analytics_queryset()
        )
        serializer_class = (
            BankerAnalyticsSerializer
            if entity_type == "banker"
            else BankAnalyticsSerializer
        )

        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        serializer = serializer_class(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    def retrieve(self, request, pk=None):
        entity_type = self._entity_type(request)
        activity_limit = self._activity_limit(request)

        if entity_type == "banker":
            entity = banker_analytics_queryset().filter(pk=pk).first()
            serializer_class = BankerAnalyticsSerializer
            activity = deal_activity_queryset(contact=entity) if entity else None
        else:
            entity = bank_analytics_queryset().filter(pk=pk).first()
            serializer_class = BankAnalyticsSerializer
            activity = deal_activity_queryset(bank=entity) if entity else None

        if entity is None:
            return Response(
                {"detail": f"{entity_type.title()} not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        entity.activity_history = list(activity[:activity_limit])
        return Response(serializer_class(entity).data)
