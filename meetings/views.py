from rest_framework import viewsets, filters
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework import status
from django_filters.rest_framework import DjangoFilterBackend
from django.db import transaction
from django.utils import timezone
from drf_spectacular.utils import extend_schema_view, extend_schema
from core.mixins import ErrorHandlingMixin
from deals.models import Deal
from .models import Meeting, MeetingContact, MeetingNote, MeetingProfile, MeetingSignalFlag
from .serializers import (
    MeetingSerializer,
    MeetingContactSerializer,
    MeetingNoteSerializer,
    MeetingProfileSerializer
)


class MeetingNotePagination(PageNumberPagination):
    page_size = 10
    page_size_query_param = 'page_size'
    max_page_size = 100


@extend_schema_view(
    list=extend_schema(
        summary="List all meetings",
        description="Retrieve a list of all meetings with optional filtering.",
        tags=["Meetings"],
    ),
    create=extend_schema(
        summary="Create a new meeting",
        description="Create a new meeting record with associated contacts and profiles.",
        tags=["Meetings"],
    ),
    retrieve=extend_schema(
        summary="Retrieve a meeting",
        description="Get detailed information about a specific meeting.",
        tags=["Meetings"],
    ),
    update=extend_schema(
        summary="Update a meeting",
        description="Update all fields of a meeting record.",
        tags=["Meetings"],
    ),
    partial_update=extend_schema(
        summary="Partially update a meeting",
        description="Update specific fields of a meeting record.",
        tags=["Meetings"],
    ),
    destroy=extend_schema(
        summary="Delete a meeting",
        description="Delete a meeting record.",
        tags=["Meetings"],
    ),
)
class MeetingViewSet(ErrorHandlingMixin, viewsets.ModelViewSet):
    queryset = Meeting.objects.prefetch_related(
        'contacts', 'profiles',
        'meeting_contacts__contact',
        'meeting_profiles__profile'
    ).all()
    serializer_class = MeetingSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['notes', 'location', 'pipeline', 'follow_ups']
    ordering_fields = ['created_at']
    ordering = ['-created_at']
    filterset_fields = ['followup_completed']


@extend_schema_view(
    list=extend_schema(
        summary="List all meeting notes",
        description="Retrieve meeting notes with optional filtering.",
        tags=["Meeting Notes"],
    ),
    create=extend_schema(
        summary="Create a meeting note",
        description="Create a meeting note, link it to deals, and index it for semantic retrieval.",
        tags=["Meeting Notes"],
    ),
    retrieve=extend_schema(
        summary="Retrieve a meeting note",
        description="Get detailed information about a specific meeting note.",
        tags=["Meeting Notes"],
    ),
    update=extend_schema(
        summary="Update a meeting note",
        description="Update a meeting note and refresh its semantic index.",
        tags=["Meeting Notes"],
    ),
    partial_update=extend_schema(
        summary="Partially update a meeting note",
        description="Update selected meeting note fields and refresh its semantic index.",
        tags=["Meeting Notes"],
    ),
    destroy=extend_schema(
        summary="Delete a meeting note",
        description="Delete a meeting note.",
        tags=["Meeting Notes"],
    ),
)
class MeetingNoteViewSet(ErrorHandlingMixin, viewsets.ModelViewSet):
    queryset = MeetingNote.objects.select_related(
        'source_email',
        'created_by',
    ).prefetch_related('deals').all()
    serializer_class = MeetingNoteSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = MeetingNotePagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['title', 'body', 'summary', 'attendees', 'action_items', 'decisions', 'location']
    ordering_fields = ['meeting_at', 'created_at', 'updated_at']
    ordering = ['-meeting_at', '-created_at']
    filterset_fields = ['source', 'is_indexed', 'deals']

    @staticmethod
    def _can_review_deal(request, deal):
        user = request.user
        if user.is_superuser or user.is_staff:
            return True
        profile = getattr(user, 'profile', None)
        return bool(
            profile
            and not profile.is_disabled
            and (profile.is_admin or deal.responsibility.filter(id=profile.id).exists())
        )

    @action(
        detail=False,
        methods=['get'],
        url_path=r'deal-signals/(?P<deal_id>[^/.]+)',
    )
    def deal_signals(self, request, deal_id=None):
        deal = Deal.objects.filter(id=deal_id).first()
        if deal is None:
            return Response({'error': 'Deal not found.'}, status=status.HTTP_404_NOT_FOUND)
        if not self._can_review_deal(request, deal):
            return Response({'error': 'Only deal-responsible analysts or administrators can view meeting signals.'}, status=status.HTTP_403_FORBIDDEN)
        queryset = MeetingSignalFlag.objects.filter(deal=deal).select_related(
            'reviewer__profile', 'first_audit_log', 'latest_audit_log'
        )
        review_status = request.query_params.get('status')
        if review_status:
            if review_status not in MeetingSignalFlag.ReviewStatus.values:
                return Response({'error': 'status must be UNREVIEWED, CONFIRMED, or DISMISSED.'}, status=400)
            queryset = queryset.filter(review_status=review_status)
        kind = request.query_params.get('kind')
        if kind:
            if kind not in MeetingSignalFlag.Kind.values:
                return Response({'error': 'kind must be RED, GREEN, or QUESTION.'}, status=400)
            queryset = queryset.filter(kind=kind)
        from .services.meeting_signal_analysis import MeetingSignalAnalysisService
        rows = [MeetingSignalAnalysisService.serialize_flag(flag) for flag in queryset]
        counts = {
            value: MeetingSignalFlag.objects.filter(deal=deal, review_status=value).count()
            for value in MeetingSignalFlag.ReviewStatus.values
        }
        return Response({'count': len(rows), 'counts': counts, 'results': rows})

    @action(
        detail=False,
        methods=['patch'],
        url_path=r'deal-signals/(?P<deal_id>[^/.]+)/(?P<signal_id>[^/.]+)',
    )
    def review_deal_signal(self, request, deal_id=None, signal_id=None):
        requested_status = str(request.data.get('review_status') or '').upper()
        comment_supplied = 'comment' in request.data
        if not requested_status and not comment_supplied:
            return Response({'error': 'review_status or comment is required.'}, status=400)
        if requested_status and requested_status not in {
            MeetingSignalFlag.ReviewStatus.CONFIRMED,
            MeetingSignalFlag.ReviewStatus.DISMISSED,
        }:
            return Response({'error': 'review_status must be CONFIRMED or DISMISSED.'}, status=400)
        with transaction.atomic():
            deal = Deal.objects.select_for_update().filter(id=deal_id).first()
            if deal is None:
                return Response({'error': 'Deal not found.'}, status=404)
            if not self._can_review_deal(request, deal):
                return Response({'error': 'Only deal-responsible analysts or administrators can review meeting signals.'}, status=403)
            flag = MeetingSignalFlag.objects.select_for_update().filter(
                id=signal_id,
                deal=deal,
            ).first()
            if flag is None:
                return Response({'error': 'Signal was not found for this deal.'}, status=404)
            expected_last_detected_at = request.data.get('expected_last_detected_at')
            if expected_last_detected_at and flag.last_detected_at.isoformat() != expected_last_detected_at:
                return Response({'error': 'The signal changed after it was loaded.'}, status=409)
            if requested_status and flag.review_status != MeetingSignalFlag.ReviewStatus.UNREVIEWED:
                return Response({'error': 'A completed signal review cannot transition to another state.'}, status=409)
            if requested_status:
                flag.review_status = requested_status
            if comment_supplied:
                flag.review_comment = str(request.data.get('comment') or '').strip()
            flag.reviewer = request.user
            flag.reviewed_at = timezone.now()
            flag.save(update_fields=['review_status', 'review_comment', 'reviewer', 'reviewed_at'])
        from .services.meeting_signal_analysis import MeetingSignalAnalysisService
        return Response(MeetingSignalAnalysisService.serialize_flag(flag))

    @extend_schema(
        summary="Re-index a meeting note",
        description="Retry chunking and embedding for an existing meeting note without changing the saved note text.",
        tags=["Meeting Notes"],
        responses={200: MeetingNoteSerializer},
    )
    @action(detail=True, methods=['post'])
    def reindex(self, request, pk=None):
        note = self.get_object()

        from ai_orchestrator.services.embedding_processor import EmbeddingService

        EmbeddingService().vectorize_meeting_note(note)
        note.refresh_from_db(fields=['is_indexed', 'chunk_count', 'embedding_error', 'updated_at'])
        response_status = status.HTTP_200_OK if note.is_indexed else status.HTTP_202_ACCEPTED
        return Response(self.get_serializer(note).data, status=response_status)

    @extend_schema(
        summary="Analyze meeting notes into red and green deal signals",
        description="Summarize a deal's meeting notes using the configured VM text model.",
        tags=["Meeting Notes"],
        request={
            'application/json': {
                'type': 'object',
                'properties': {'deal_id': {'type': 'string'}},
                'required': ['deal_id'],
            }
        },
        responses={200: dict},
    )
    @action(detail=False, methods=['post'])
    def analyze_deal_signals(self, request):
        deal_id = request.data.get('deal_id') or request.query_params.get('deal_id')
        if not deal_id:
            return Response(
                {'error': 'Validation failed', 'details': {'deal_id': ['This field is required.']}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        deal = Deal.objects.filter(id=deal_id).first()
        if not deal:
            return Response(
                {'error': 'Deal not found', 'details': f'No deal found for id {deal_id}'},
                status=status.HTTP_404_NOT_FOUND,
            )

        notes = list(
            MeetingNote.objects.filter(deals=deal)
            .select_related('source_email')
            .prefetch_related('deals')
            .order_by('meeting_at', 'created_at')[:25]
        )

        try:
            from .services.meeting_signal_analysis import MeetingSignalAnalysisService

            result = MeetingSignalAnalysisService().analyze_deal(deal, notes)
            return Response(result, status=status.HTTP_200_OK)
        except Exception as exc:
            return Response(
                {
                    'error': 'Meeting signal analysis failed',
                    'details': str(exc),
                    'provider': 'vllm',
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )


@extend_schema_view(
    list=extend_schema(
        summary="List all meeting-contact relationships",
        description="Retrieve all relationships between meetings and contacts.",
        tags=["Meetings"],
    ),
    create=extend_schema(
        summary="Create meeting-contact relationship",
        description="Associate a contact with a meeting.",
        tags=["Meetings"],
    ),
    retrieve=extend_schema(
        summary="Retrieve meeting-contact relationship",
        description="Get a specific meeting-contact relationship.",
        tags=["Meetings"],
    ),
    destroy=extend_schema(
        summary="Remove meeting-contact relationship",
        description="Remove a contact from a meeting.",
        tags=["Meetings"],
    ),
)
class MeetingContactViewSet(ErrorHandlingMixin, viewsets.ModelViewSet):
    queryset = MeetingContact.objects.select_related('meeting', 'contact').all()
    serializer_class = MeetingContactSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    ordering_fields = ['id']
    filterset_fields = ['meeting', 'contact']


@extend_schema_view(
    list=extend_schema(
        summary="List all meeting-profile relationships",
        description="Retrieve all relationships between meetings and profiles.",
        tags=["Meetings"],
    ),
    create=extend_schema(
        summary="Create meeting-profile relationship",
        description="Associate a profile with a meeting.",
        tags=["Meetings"],
    ),
    retrieve=extend_schema(
        summary="Retrieve meeting-profile relationship",
        description="Get a specific meeting-profile relationship.",
        tags=["Meetings"],
    ),
    destroy=extend_schema(
        summary="Remove meeting-profile relationship",
        description="Remove a profile from a meeting.",
        tags=["Meetings"],
    ),
)
class MeetingProfileViewSet(ErrorHandlingMixin, viewsets.ModelViewSet):
    queryset = MeetingProfile.objects.select_related('meeting', 'profile').all()
    serializer_class = MeetingProfileSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    ordering_fields = ['id']
    filterset_fields = ['meeting', 'profile']
