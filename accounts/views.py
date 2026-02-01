from rest_framework import viewsets, filters
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema, extend_schema_view
from core.mixins import ErrorHandlingMixin
from .models import Profile
from .serializers import ProfileSerializer, ProfileListSerializer


@extend_schema_view(
    list=extend_schema(
        summary="List all profiles",
        description="Retrieve a list of all user profiles with optional filtering.",
        tags=["Profiles"],
    ),
    create=extend_schema(
        summary="Create a new profile",
        description="Create a new user profile record.",
        tags=["Profiles"],
    ),
    retrieve=extend_schema(
        summary="Retrieve a profile",
        description="Get detailed information about a specific profile.",
        tags=["Profiles"],
    ),
    update=extend_schema(
        summary="Update a profile",
        description="Update all fields of a profile record.",
        tags=["Profiles"],
    ),
    partial_update=extend_schema(
        summary="Partially update a profile",
        description="Update specific fields of a profile record.",
        tags=["Profiles"],
    ),
    destroy=extend_schema(
        summary="Delete a profile",
        description="Delete a profile record.",
        tags=["Profiles"],
    ),
)
class ProfileViewSet(ErrorHandlingMixin, viewsets.ModelViewSet):
    queryset = Profile.objects.all()
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['name', 'email']
    ordering_fields = ['name', 'email', 'created_at']
    ordering = ['name']
    filterset_fields = ['is_admin', 'is_disabled']
    
    def get_serializer_class(self):
        # Use lightweight serializer for list to reduce response size
        if self.action == 'list':
            return ProfileListSerializer
        return ProfileSerializer
