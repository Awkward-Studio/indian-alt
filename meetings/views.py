from rest_framework import viewsets, filters
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework import status
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema_view, extend_schema
from core.mixins import ErrorHandlingMixin
from .models import Meeting, MeetingContact, MeetingNote, MeetingProfile
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
