import logging
import uuid
from rest_framework import viewsets, filters, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.views import APIView
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema, extend_schema_view, OpenApiParameter
from drf_spectacular.types import OpenApiTypes
from core.mixins import ErrorHandlingMixin
from .models import Version
from .serializers import VersionSerializer

logger = logging.getLogger(__name__)


class HealthCheckView(APIView):
    """
    Lightweight health check endpoint for deploy platforms (e.g., Railway).
    Intentionally unauthenticated.
    """

    permission_classes = [AllowAny]

    def get(self, request):
        return Response({"status": "ok"}, status=status.HTTP_200_OK)


@extend_schema_view(
    list=extend_schema(
        summary="List all versions",
        description="Retrieve a list of all version/audit history records.",
        tags=["Versions"],
    ),
    retrieve=extend_schema(
        summary="Retrieve a version",
        description="Get detailed information about a specific version record.",
        tags=["Versions"],
    ),
)
class VersionViewSet(ErrorHandlingMixin, viewsets.ReadOnlyModelViewSet):
    # Read-only because versions are created by database triggers, not via API
    queryset = Version.objects.all()
    serializer_class = VersionSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    ordering_fields = ['created_at']
    ordering = ['-created_at']
    filterset_fields = ['item_id', 'type', 'user_id']
    
    @extend_schema(
        summary="Get versions for a specific item",
        description="Retrieve version history for a specific deal or contact.",
        tags=["Versions"],
        parameters=[
            OpenApiParameter(
                name='item_id',
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.QUERY,
                required=True,
                description='UUID of the item (deal or contact)'
            ),
            OpenApiParameter(
                name='type',
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                required=False,
                description='Type of item: "deal" or "contact"',
                enum=['deal', 'contact']
            ),
        ],
        responses={200: VersionSerializer(many=True)},
    )
    @action(detail=False, methods=['get'])
    def by_item(self, request):
        # Custom endpoint to fetch version history for a specific deal or contact
        try:
            item_id = request.query_params.get('item_id')
            item_type = request.query_params.get('type')
            
            if not item_id:
                return Response(
                    {
                        'error': 'Validation failed',
                        'details': {'item_id': ['This parameter is required']}
                    },
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Validate UUID format
            try:
                uuid.UUID(item_id)
            except ValueError:
                return Response(
                    {
                        'error': 'Validation failed',
                        'details': {'item_id': ['Invalid UUID format']}
                    },
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            queryset = self.get_queryset().filter(item_id=item_id)
            
            if item_type:
                if item_type not in ['deal', 'contact']:
                    return Response(
                        {
                            'error': 'Validation failed',
                            'details': {'type': ['Must be "deal" or "contact"']}
                        },
                        status=status.HTTP_400_BAD_REQUEST
                    )
                queryset = queryset.filter(type=item_type)
            
            serializer = self.get_serializer(queryset, many=True)
            return Response(serializer.data, status=status.HTTP_200_OK)
            
        except Exception as e:
            logger.error(f"Error in by_item: {str(e)}")
            return Response(
                {
                    'error': 'Failed to retrieve versions',
                    'details': str(e)
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
