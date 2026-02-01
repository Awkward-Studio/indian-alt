from rest_framework import viewsets, filters, status
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema, extend_schema_view
from drf_spectacular.types import OpenApiTypes
from core.mixins import ErrorHandlingMixin
from .models import Bank
from .serializers import BankSerializer


@extend_schema_view(
    list=extend_schema(
        summary="List all banks",
        description="Retrieve a list of all banks with optional filtering and search.",
        tags=["Banks"],
    ),
    create=extend_schema(
        summary="Create a new bank",
        description="Create a new bank record.",
        tags=["Banks"],
    ),
    retrieve=extend_schema(
        summary="Retrieve a bank",
        description="Get detailed information about a specific bank.",
        tags=["Banks"],
    ),
    update=extend_schema(
        summary="Update a bank",
        description="Update all fields of a bank record.",
        tags=["Banks"],
    ),
    partial_update=extend_schema(
        summary="Partially update a bank",
        description="Update specific fields of a bank record.",
        tags=["Banks"],
    ),
    destroy=extend_schema(
        summary="Delete a bank",
        description="Delete a bank record. This will fail if the bank has associated contacts or deals.",
        tags=["Banks"],
    ),
)
class BankViewSet(ErrorHandlingMixin, viewsets.ModelViewSet):
    queryset = Bank.objects.all()
    serializer_class = BankSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['name']
    ordering_fields = ['name', 'created_at']
    ordering = ['name']
