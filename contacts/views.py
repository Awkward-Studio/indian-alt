from rest_framework import viewsets, filters
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from django_filters import FilterSet, CharFilter
from drf_spectacular.utils import extend_schema_view, extend_schema
from core.mixins import ErrorHandlingMixin
from .models import Contact
from .serializers import ContactSerializer, ContactListSerializer


class ContactFilterSet(FilterSet):
    """Custom filter set for Contact model to handle ArrayField."""
    # Use 'contains' lookup for ArrayField instead of 'exact'
    sector_coverage = CharFilter(field_name='sector_coverage', lookup_expr='contains')
    
    class Meta:
        model = Contact
        fields = ['bank', 'sector_coverage']


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
    queryset = Contact.objects.select_related('bank').all()
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['name', 'email', 'designation', 'location']
    ordering_fields = ['name', 'email', 'created_at']
    ordering = ['-created_at']
    filterset_class = ContactFilterSet
    
    def get_serializer_class(self):
        # Use lightweight serializer for list to reduce response size
        if self.action == 'list':
            return ContactListSerializer
        return ContactSerializer
