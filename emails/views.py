"""
Views for email management API endpoints.
"""
import logging
from rest_framework import viewsets, filters, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema, extend_schema_view, OpenApiParameter
from drf_spectacular.types import OpenApiTypes
from core.mixins import ErrorHandlingMixin
from .models import EmailAccount, Email
from .serializers import (
    EmailAccountSerializer,
    EmailAccountListSerializer,
    EmailSerializer,
    EmailListSerializer,
    EmailFetchSerializer
)
from .services.email_reader import EmailReaderService
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


@extend_schema_view(
    list=extend_schema(
        summary="List all email accounts",
        description="Retrieve a list of all email accounts being monitored.",
        tags=["Email Accounts"],
    ),
    create=extend_schema(
        summary="Add email account to monitor",
        description="Add a new email account to the monitoring list.",
        tags=["Email Accounts"],
    ),
    retrieve=extend_schema(
        summary="Retrieve an email account",
        description="Get detailed information about a specific email account.",
        tags=["Email Accounts"],
    ),
    update=extend_schema(
        summary="Update an email account",
        description="Update an email account configuration.",
        tags=["Email Accounts"],
    ),
    partial_update=extend_schema(
        summary="Partially update an email account",
        description="Update specific fields of an email account.",
        tags=["Email Accounts"],
    ),
    destroy=extend_schema(
        summary="Remove email account",
        description="Remove an email account from monitoring (does not delete emails).",
        tags=["Email Accounts"],
    ),
)
class EmailAccountViewSet(ErrorHandlingMixin, viewsets.ModelViewSet):
    """ViewSet for managing email accounts to monitor."""
    
    queryset = EmailAccount.objects.all()
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['email']
    ordering_fields = ['email', 'created_at', 'last_synced']
    ordering = ['email']
    filterset_fields = ['is_active']
    
    def get_serializer_class(self):
        if self.action == 'list':
            return EmailAccountListSerializer
        return EmailAccountSerializer


@extend_schema_view(
    list=extend_schema(
        summary="List all emails",
        description="Retrieve a list of all emails with optional filtering and search.",
        tags=["Emails"],
    ),
    retrieve=extend_schema(
        summary="Retrieve an email",
        description="Get detailed information about a specific email.",
        tags=["Emails"],
    ),
)
class EmailViewSet(ErrorHandlingMixin, viewsets.ReadOnlyModelViewSet):
    """ViewSet for viewing emails (read-only)."""
    
    queryset = Email.objects.select_related('email_account').all()
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['subject', 'from_email', 'body_text', 'body_preview']
    ordering_fields = ['date_received', 'date_sent', 'created_at', 'subject']
    ordering = ['-date_received', '-created_at']
    filterset_fields = [
        'email_account',
        'is_read',
        'is_processed',
        'importance',
        'has_attachments'
    ]
    
    def get_serializer_class(self):
        if self.action == 'list':
            return EmailListSerializer
        return EmailSerializer
    
    @extend_schema(
        summary="Get emails for a specific account",
        description="Retrieve all emails for a specific email account.",
        tags=["Emails"],
        parameters=[
            OpenApiParameter(
                name='email',
                type=OpenApiTypes.EMAIL,
                location=OpenApiParameter.QUERY,
                required=True,
                description='Email address of the account'
            ),
        ],
        responses={200: EmailListSerializer(many=True)},
    )
    @action(detail=False, methods=['get'])
    def by_account(self, request):
        """Get emails for a specific email account."""
        try:
            email = request.query_params.get('email')
            if not email:
                return Response(
                    {
                        'error': 'Validation failed',
                        'details': {'email': ['This parameter is required']}
                    },
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Find email account
            try:
                email_account = EmailAccount.objects.get(email=email)
            except EmailAccount.DoesNotExist:
                return Response(
                    {
                        'error': 'Email account not found',
                        'details': f'No account found for {email}'
                    },
                    status=status.HTTP_404_NOT_FOUND
                )
            
            queryset = self.get_queryset().filter(email_account=email_account)
            serializer = self.get_serializer(queryset, many=True)
            return Response(serializer.data, status=status.HTTP_200_OK)
            
        except Exception as e:
            logger.error(f"Error in by_account: {str(e)}")
            return Response(
                {
                    'error': 'Failed to retrieve emails',
                    'details': str(e)
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class EmailFetchViewSet(ErrorHandlingMixin, viewsets.ViewSet):
    """ViewSet for triggering email fetching."""
    
    permission_classes = [IsAuthenticated]
    serializer_class = EmailFetchSerializer
    
    @extend_schema(
        summary="Fetch emails for all active accounts",
        description="Manually trigger email fetching for all active email accounts.",
        tags=["Email Fetching"],
        parameters=[
            OpenApiParameter(
                name='limit',
                type=OpenApiTypes.INT,
                location=OpenApiParameter.QUERY,
                required=False,
                description='Maximum number of emails to fetch per account'
            ),
            OpenApiParameter(
                name='since',
                type=OpenApiTypes.DATETIME,
                location=OpenApiParameter.QUERY,
                required=False,
                description='Only fetch emails received after this date (ISO format)'
            ),
        ],
        responses={200: EmailFetchSerializer},
    )
    @action(detail=False, methods=['post'])
    def fetch_all(self, request):
        """Fetch emails for all active accounts."""
        try:
            limit = request.query_params.get('limit')
            since_str = request.query_params.get('since')
            
            limit = int(limit) if limit else None
            since = None
            if since_str:
                try:
                    since = datetime.fromisoformat(since_str.replace('Z', '+00:00'))
                except ValueError:
                    return Response(
                        {
                            'error': 'Validation failed',
                            'details': {'since': ['Invalid datetime format. Use ISO format.']}
                        },
                        status=status.HTTP_400_BAD_REQUEST
                    )
            
            email_reader = EmailReaderService()
            results = email_reader.fetch_all_active_accounts(
                limit_per_account=limit,
                since=since
            )
            
            serializer = EmailFetchSerializer(results)
            return Response(serializer.data, status=status.HTTP_200_OK)
            
        except ValueError as e:
            return Response(
                {
                    'error': 'Validation failed',
                    'details': {'limit': ['Must be a valid integer']}
                },
                status=status.HTTP_400_BAD_REQUEST
            )
        except Exception as e:
            logger.error(f"Error in fetch_all: {str(e)}", exc_info=True)
            return Response(
                {
                    'error': 'Failed to fetch emails',
                    'details': str(e)
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    @extend_schema(
        summary="Fetch emails for a specific account",
        description="Manually trigger email fetching for a specific email account.",
        tags=["Email Fetching"],
        parameters=[
            OpenApiParameter(
                name='email',
                type=OpenApiTypes.EMAIL,
                location=OpenApiParameter.QUERY,
                required=True,
                description='Email address of the account to fetch'
            ),
            OpenApiParameter(
                name='limit',
                type=OpenApiTypes.INT,
                location=OpenApiParameter.QUERY,
                required=False,
                description='Maximum number of emails to fetch'
            ),
            OpenApiParameter(
                name='since',
                type=OpenApiTypes.DATETIME,
                location=OpenApiParameter.QUERY,
                required=False,
                description='Only fetch emails received after this date (ISO format)'
            ),
        ],
        responses={200: EmailFetchSerializer},
    )
    @action(detail=False, methods=['post'], url_path='fetch/(?P<email>[^/.]+)')
    def fetch_account(self, request, email=None):
        """Fetch emails for a specific email account."""
        try:
            if not email:
                return Response(
                    {
                        'error': 'Validation failed',
                        'details': {'email': ['Email parameter is required']}
                    },
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            try:
                email_account = EmailAccount.objects.get(email=email)
            except EmailAccount.DoesNotExist:
                return Response(
                    {
                        'error': 'Email account not found',
                        'details': f'No account found for {email}. Add it first via /api/emails/accounts/'
                    },
                    status=status.HTTP_404_NOT_FOUND
                )
            
            limit = request.query_params.get('limit')
            since_str = request.query_params.get('since')
            
            limit = int(limit) if limit else None
            since = None
            if since_str:
                try:
                    since = datetime.fromisoformat(since_str.replace('Z', '+00:00'))
                except ValueError:
                    return Response(
                        {
                            'error': 'Validation failed',
                            'details': {'since': ['Invalid datetime format. Use ISO format.']}
                        },
                        status=status.HTTP_400_BAD_REQUEST
                    )
            
            email_reader = EmailReaderService()
            result = email_reader.fetch_emails_for_account(
                email_account=email_account,
                limit=limit,
                since=since
            )
            
            serializer = EmailFetchSerializer(result)
            return Response(serializer.data, status=status.HTTP_200_OK)
            
        except ValueError as e:
            return Response(
                {
                    'error': 'Validation failed',
                    'details': {'limit': ['Must be a valid integer']}
                },
                status=status.HTTP_400_BAD_REQUEST
            )
        except Exception as e:
            logger.error(f"Error in fetch_account: {str(e)}", exc_info=True)
            return Response(
                {
                    'error': 'Failed to fetch emails',
                    'details': str(e)
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
