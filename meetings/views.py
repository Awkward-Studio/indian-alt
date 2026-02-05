from rest_framework import viewsets, filters
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema_view, extend_schema
from core.mixins import ErrorHandlingMixin
from .models import Meeting, MeetingContact, MeetingProfile
from .serializers import (
    MeetingSerializer,
    MeetingContactSerializer,
    MeetingProfileSerializer
)


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
