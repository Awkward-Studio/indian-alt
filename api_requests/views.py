from rest_framework import viewsets, filters
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema_view, extend_schema
from core.mixins import ErrorHandlingMixin
from .models import Request
from .serializers import RequestSerializer


@extend_schema_view(
    list=extend_schema(
        summary="List all requests",
        description="Retrieve a list of all requests with optional filtering.",
        tags=["Requests"],
    ),
    create=extend_schema(
        summary="Create a new request",
        description="Create a new request record.",
        tags=["Requests"],
    ),
    retrieve=extend_schema(
        summary="Retrieve a request",
        description="Get detailed information about a specific request.",
        tags=["Requests"],
    ),
    update=extend_schema(
        summary="Update a request",
        description="Update all fields of a request record.",
        tags=["Requests"],
    ),
    partial_update=extend_schema(
        summary="Partially update a request",
        description="Update specific fields of a request record.",
        tags=["Requests"],
    ),
    destroy=extend_schema(
        summary="Delete a request",
        description="Delete a request record.",
        tags=["Requests"],
    ),
)
class RequestViewSet(ErrorHandlingMixin, viewsets.ModelViewSet):
    queryset = Request.objects.all()
    serializer_class = RequestSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['logs']
    ordering_fields = ['created_at', 'status']
    ordering = ['-created_at']
    filterset_fields = ['status']
