"""
Views for Microsoft Graph API endpoints — email management and OneDrive.
"""
import base64
import logging
from datetime import datetime, timedelta

from django.utils import timezone
from rest_framework import viewsets, filters, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView
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
    EmailFetchSerializer,
    DriveItemSerializer,
    OneDriveListResponseSerializer,
)
from .services.email_reader import EmailReaderService
from .services.graph_service import GraphAPIService, DMS_USER_EMAIL, DMS_DRIVE_ID
from ai_orchestrator.services.ai_processor import AIProcessorService
from ai_orchestrator.services.document_processor import DocumentProcessorService
from ai_orchestrator.services.embedding_processor import EmbeddingService

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
        summary="Analyze an email with AI",
        description="Process the content of a specific email using the AI orchestration service. Also processes attachments (PDF, Excel, Images) if present.",
        tags=["Emails"],
        responses={200: OpenApiTypes.OBJECT},
    )
    @action(detail=True, methods=['post'])
    def analyze(self, request, pk=None):
        """Queue async AI analysis for a specific email and its attachments."""
        try:
            email = self.get_object()
            from .tasks import analyze_email_async
            from ai_orchestrator.models import AIAuditLog, AIPersonality, AISkill

            existing_run = AIAuditLog.objects.filter(
                source_type='email',
                source_id=str(email.id),
                status__in=['PENDING', 'PROCESSING'],
            ).order_by('-created_at').first()
            if existing_run:
                return Response({
                    "task_id": existing_run.celery_task_id,
                    "audit_log_id": str(existing_run.id),
                    "status": "processing",
                    "message": "An email analysis is already running for this signal.",
                }, status=status.HTTP_200_OK)

            personality = AIPersonality.objects.filter(is_default=True).first()
            skill = AISkill.objects.filter(name='deal_extraction').first()
            default_model = personality.text_model_name if personality else 'qwen3.5:latest'

            audit_log = AIAuditLog.objects.create(
                source_type='email',
                source_id=str(email.id),
                context_label=f"Email: {email.subject}",
                personality=personality,
                skill=skill,
                status='PENDING',
                is_success=False,
                model_used=default_model,
                system_prompt="Queued forensic email analysis...",
                user_prompt=f"Analyzing email signal: {email.subject}",
                source_metadata={
                    "email_id": str(email.id),
                    "subject": email.subject,
                    "email_account": email.email_account.email if email.email_account else None,
                    "has_attachments": bool(email.has_attachments),
                    "attachment_count": len(email.attachments if isinstance(email.attachments, list) else []),
                },
            )

            task = analyze_email_async.apply_async(
                kwargs={
                    "email_id": str(email.id),
                    "audit_log_id": str(audit_log.id),
                },
                queue='low_priority'
            )
            audit_log.celery_task_id = task.id
            audit_log.save(update_fields=['celery_task_id'])

            return Response({
                "task_id": task.id,
                "audit_log_id": str(audit_log.id),
                "status": "queued",
                "message": "Email analysis queued."
            }, status=status.HTTP_200_OK)
            
        except Exception as e:
            logger.error(f"Error in analyze email: {str(e)}", exc_info=True)
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @extend_schema(
        summary='Process contacts from all existing emails',
        description='Iterate through all stored emails and create contacts from From/CC fields if they do not exist.',
        tags=['Emails'],
        responses={200: {'type': 'object', 'properties': {'success': {'type': 'boolean'}, 'new_count': {'type': 'integer'}}}},
    )
    @action(detail=False, methods=['post'])
    def process_contacts(self, request):
        """Process all existing emails to extract contacts."""
        try:
            from contacts.models import Contact
            emails = Email.objects.all()
            new_count = 0
            
            for email in emails:
                addresses = []
                if email.from_email: addresses.append(email.from_email)
                if email.cc_emails: addresses.extend(email.cc_emails)
                
                for addr in addresses:
                    if not addr or '@' not in addr: continue
                    addr_lower = addr.lower().strip()
                    
                    if not Contact.objects.filter(email__iexact=addr_lower).exists():
                        try:
                            Contact.objects.create(
                                name=addr_lower.split('@')[0],
                                email=addr_lower,
                                designation='Auto-created from Archive'
                            )
                            new_count += 1
                        except Exception as e:
                            logger.error(f'Failed to auto-create contact {addr_lower}: {str(e)}')
                    
            return Response({'success': True, 'new_count': new_count}, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(f'Global error in process_contacts: {str(e)}')
            return Response(
                {
                    'error': 'Failed to process contacts',
                    'details': str(e),
                    'status_code': status.HTTP_500_INTERNAL_SERVER_ERROR,
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

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
                description='Email address of the account',
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
            OpenApiParameter(
                name='search',
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                required=False,
                description='Search query for Graph API ($search)'
            ),
            OpenApiParameter(
                name='return_emails',
                type=OpenApiTypes.BOOL,
                location=OpenApiParameter.QUERY,
                required=False,
                description='Whether to return the list of fetched emails'
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
            search_query = request.query_params.get('search')
            return_emails = request.query_params.get('return_emails', 'false').lower() == 'true'
            
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
                since=since,
                search_query=search_query,
                return_emails=return_emails
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
        summary="Sync and return emails",
        description="Fetch emails for all active accounts and return the fetched email objects.",
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
                name='search',
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                required=False,
                description='Search query for Graph API ($search)'
            ),
        ],
        responses={200: EmailFetchSerializer},
    )
    @action(detail=False, methods=['post'])
    def sync(self, request):
        """Trigger sync and return emails."""
        try:
            limit = request.query_params.get('limit')
            search_query = request.query_params.get('search')
            limit = int(limit) if limit else 50  # Default to 50 for sync
            
            email_reader = EmailReaderService()
            results = email_reader.fetch_all_active_accounts(
                limit_per_account=limit,
                search_query=search_query,
                return_emails=True
            )
            
            serializer = EmailFetchSerializer(results)
            return Response(serializer.data, status=status.HTTP_200_OK)
            
        except Exception as e:
            logger.error(f"Error in sync: {str(e)}", exc_info=True)
            return Response(
                {
                    'error': 'Failed to sync emails',
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
            OpenApiParameter(
                name='search',
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                required=False,
                description='Search query for Graph API ($search)'
            ),
            OpenApiParameter(
                name='return_emails',
                type=OpenApiTypes.BOOL,
                location=OpenApiParameter.QUERY,
                required=False,
                description='Whether to return the list of fetched emails'
            ),
        ],
        responses={200: EmailFetchSerializer},
    )
    @action(detail=False, methods=['post'], url_path='fetch/(?P<email>[^/]+)')
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
                        'details': f'No account found for {email}. Add it first via /api/microsoft/emails/accounts/'
                    },
                    status=status.HTTP_404_NOT_FOUND
                )
            
            limit = request.query_params.get('limit')
            since_str = request.query_params.get('since')
            search_query = request.query_params.get('search')
            return_emails = request.query_params.get('return_emails', 'false').lower() == 'true'
            
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
                since=since,
                search_query=search_query,
                return_emails=return_emails
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


ONEDRIVE_MOCK_DATA = [
    {
        'id': 'mock-folder-001',
        'name': 'Documents',
        'size': 0,
        'webUrl': 'https://onedrive.live.com/documents',
        'createdDateTime': '2025-06-01T09:00:00Z',
        'lastModifiedDateTime': '2026-02-15T14:30:00Z',
        'file': None,
        'folder': {'childCount': 12},
        'parentReference': {
            'driveId': 'mock-drive-id',
            'driveType': 'business',
            'id': 'mock-root-id',
            'path': '/drive/root:',
        },
    },
    {
        'id': 'mock-folder-002',
        'name': 'Deal Files',
        'size': 0,
        'webUrl': 'https://onedrive.live.com/deal-files',
        'createdDateTime': '2025-08-10T11:00:00Z',
        'lastModifiedDateTime': '2026-02-10T08:45:00Z',
        'file': None,
        'folder': {'childCount': 5},
        'parentReference': {
            'driveId': 'mock-drive-id',
            'driveType': 'business',
            'id': 'mock-root-id',
            'path': '/drive/root:',
        },
    },
    {
        'id': 'mock-file-001',
        'name': 'Q4-Report-2025.pdf',
        'size': 2_450_000,
        'webUrl': 'https://onedrive.live.com/q4-report',
        'createdDateTime': '2026-01-05T16:20:00Z',
        'lastModifiedDateTime': '2026-01-05T16:20:00Z',
        'file': {'mimeType': 'application/pdf'},
        'folder': None,
        'parentReference': {
            'driveId': 'mock-drive-id',
            'driveType': 'business',
            'id': 'mock-root-id',
            'path': '/drive/root:',
        },
    },
    {
        'id': 'mock-file-002',
        'name': 'Contact List.xlsx',
        'size': 185_000,
        'webUrl': 'https://onedrive.live.com/contact-list',
        'createdDateTime': '2025-12-20T10:00:00Z',
        'lastModifiedDateTime': '2026-02-14T09:15:00Z',
        'file': {'mimeType': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'},
        'folder': None,
        'parentReference': {
            'driveId': 'mock-drive-id',
            'driveType': 'business',
            'id': 'mock-root-id',
            'path': '/drive/root:',
        },
    },
    {
        'id': 'mock-file-003',
        'name': 'Meeting Notes.docx',
        'size': 45_000,
        'webUrl': 'https://onedrive.live.com/meeting-notes',
        'createdDateTime': '2026-02-01T13:00:00Z',
        'lastModifiedDateTime': '2026-02-18T11:30:00Z',
        'file': {'mimeType': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'},
        'folder': None,
        'parentReference': {
            'driveId': 'mock-drive-id',
            'driveType': 'business',
            'id': 'mock-root-id',
            'path': '/drive/root:',
        },
    },
]


class OneDriveListView(APIView):
    """
    Browse files and folders in the DMS shared folder on SharePoint/OneDrive.

    - Omit ``folder_id`` → lists the root of the DMS shared folder.
    - Supply ``folder_id`` → lists children of that specific folder.
    - Use ``top`` to limit results (Graph API handles its own cursor-based pagination).
    - Pass ``mock=true`` to get sample data without hitting Azure.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="List OneDrive files and folders",
        description=(
            "Browse the DMS shared folder via Microsoft Graph API.\n\n"
            "- Omit `folder_id` to list the **root** of the shared folder.\n"
            "- Supply `folder_id` to drill into a subfolder.\n"
            "- Use `top` to limit the number of items returned.\n"
            "- Pass `mock=true` for sample data."
        ),
        tags=["OneDrive"],
        parameters=[
            OpenApiParameter(
                name='user_email',
                type=OpenApiTypes.EMAIL,
                location=OpenApiParameter.QUERY,
                required=False,
                description='Microsoft account email to use for delegated OneDrive access (defaults to DMS account)',
            ),
            OpenApiParameter(
                name='folder_id',
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                required=False,
                description='ID of a subfolder to list (omit for root of DMS shared folder)',
            ),
            OpenApiParameter(
                name='top',
                type=OpenApiTypes.INT,
                location=OpenApiParameter.QUERY,
                required=False,
                description='Maximum number of items to return (default 100)',
            ),
            OpenApiParameter(
                name='mock',
                type=OpenApiTypes.BOOL,
                location=OpenApiParameter.QUERY,
                required=False,
                description='Return sample mock data instead of calling Azure',
            ),
        ],
        responses={200: OneDriveListResponseSerializer},
    )
    def get(self, request):
        """List files and folders from the DMS shared folder."""
        user_email = request.query_params.get('user_email') or DMS_USER_EMAIL
        folder_id = request.query_params.get('folder_id')
        top = request.query_params.get('top', 100)
        use_mock = request.query_params.get('mock', '').lower() in ('true', '1', 'yes')

        try:
            top = int(top)
        except (ValueError, TypeError):
            return Response(
                {
                    'error': 'Validation failed',
                    'details': {'top': ['Must be a valid integer']},
                    'status_code': status.HTTP_400_BAD_REQUEST,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # ---- mock mode ----
        if use_mock:
            items = ONEDRIVE_MOCK_DATA[:top]
            response_data = {
                'count': len(items),
                'items': items,
                'next_skip': None,
            }
            serializer = OneDriveListResponseSerializer(response_data)
            return Response(serializer.data, status=status.HTTP_200_OK)

        # ---- call Graph API ----
        try:
            graph = GraphAPIService()
            drive_id = request.query_params.get('drive_id')

            if folder_id:
                data = graph.get_drive_folder_children(
                    user_email=user_email,
                    folder_id=folder_id,
                    drive_id=drive_id,
                    top=top,
                )
            else:
                try:
                    # Attempt to list all shared folders
                    data = graph.list_shared_with_me(
                        user_email=user_email,
                        top=top,
                    )
                except Exception as e:
                    # If sharedWithMe fails (e.g., account not provisioned), fall back to configured DMS root
                    logger.warning(f"sharedWithMe failed ({e}), falling back to DMS root children.")
                    data = graph.get_drive_root_children(
                        user_email=user_email,
                        top=top,
                    )

            # Note: Items from /sharedWithMe are 'remoteItem' in some cases
            items = []
            for item in data.get('value', []):
                if 'remoteItem' in item:
                    # Flatten remote item metadata so the frontend sees it as a normal DriveItem
                    remote = item['remoteItem']
                    # Preserve IDs
                    remote['id'] = remote.get('id', item.get('id'))
                    # Crucially: capture the driveId from the remote reference
                    if 'parentReference' in remote:
                        remote['driveId'] = remote['parentReference'].get('driveId')
                    
                    # Ensure name and types match
                    remote['name'] = remote.get('name', item.get('name'))
                    items.append(remote)
                else:
                    items.append(item)

            next_link = data.get('@odata.nextLink')

            response_data = {
                'count': len(items),
                'items': items,
                'next_skip': top if next_link else None,
            }

            serializer = OneDriveListResponseSerializer(response_data)
            return Response(serializer.data, status=status.HTTP_200_OK)

        except ValueError as e:
            # Authentication/permission errors
            logger.error(f"OneDrive authentication error: {e}", exc_info=True)
            return Response(
                {
                    'error': 'Authentication required',
                    'details': str(e),
                    'status_code': status.HTTP_401_UNAUTHORIZED,
                },
                status=status.HTTP_401_UNAUTHORIZED,
            )
        except Exception as e:
            logger.error(f"OneDrive list error: {e}", exc_info=True)
            return Response(
                {
                    'error': 'Failed to fetch OneDrive items',
                    'details': str(e),
                    'status_code': status.HTTP_500_INTERNAL_SERVER_ERROR,
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class OneDriveFileDetailView(APIView):
    """Get metadata for a specific file/folder in the DMS shared drive."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Get file/folder details",
        description="Returns metadata (name, size, dates, download URL) for a specific item.",
        tags=["OneDrive"],
        parameters=[
            OpenApiParameter(name='item_id', type=OpenApiTypes.STR, location=OpenApiParameter.QUERY, required=True),
        ],
        responses={200: DriveItemSerializer},
    )
    def get(self, request):
        """Retrieve metadata for a single drive item."""
        item_id = request.query_params.get('item_id')
        if not item_id:
            return Response(
                {
                    'error': 'Validation failed',
                    'details': {'item_id': ['This query parameter is required']},
                    'status_code': status.HTTP_400_BAD_REQUEST,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            graph = GraphAPIService()
            item = graph.get_drive_item(DMS_DRIVE_ID, item_id)
            serializer = DriveItemSerializer(item)
            return Response(serializer.data, status=status.HTTP_200_OK)
        except ValueError as e:
            # Authentication/permission errors
            logger.error(f"OneDrive authentication error: {e}", exc_info=True)
            return Response(
                {
                    'error': 'Authentication required',
                    'details': str(e),
                    'status_code': status.HTTP_401_UNAUTHORIZED,
                },
                status=status.HTTP_401_UNAUTHORIZED,
            )
        except Exception as e:
            logger.error(f"OneDrive detail error: {e}", exc_info=True)
            return Response(
                {
                    'error': 'Failed to retrieve item details',
                    'details': str(e),
                    'status_code': status.HTTP_500_INTERNAL_SERVER_ERROR,
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class OneDriveDownloadView(APIView):
    """Get a temporary download URL for a file in the DMS shared drive."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Get file download URL",
        description="Returns a short-lived pre-authenticated download URL for a file.",
        tags=["OneDrive"],
        parameters=[
            OpenApiParameter(name='item_id', type=OpenApiTypes.STR, location=OpenApiParameter.QUERY, required=True),
        ],
        responses={200: OpenApiTypes.OBJECT},
    )
    def get(self, request):
        """Return a short-lived pre-authenticated download URL."""
        item_id = request.query_params.get('item_id')
        if not item_id:
            return Response(
                {
                    'error': 'Validation failed',
                    'details': {'item_id': ['This query parameter is required']},
                    'status_code': status.HTTP_400_BAD_REQUEST,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            graph = GraphAPIService()
            download_url = graph.get_drive_item_download_url(DMS_DRIVE_ID, item_id)
            if not download_url:
                return Response(
                    {
                        'error': 'Resource not found',
                        'details': 'Could not obtain a download URL for this item',
                        'status_code': status.HTTP_404_NOT_FOUND,
                    },
                    status=status.HTTP_404_NOT_FOUND,
                )
            return Response({'download_url': download_url}, status=status.HTTP_200_OK)
        except ValueError as e:
            # Authentication/permission errors
            logger.error(f"OneDrive authentication error: {e}", exc_info=True)
            return Response(
                {
                    'error': 'Authentication required',
                    'details': str(e),
                    'status_code': status.HTTP_401_UNAUTHORIZED,
                },
                status=status.HTTP_401_UNAUTHORIZED,
            )
        except Exception as e:
            logger.error(f"OneDrive download error: {e}", exc_info=True)
            return Response(
                {
                    'error': 'Failed to generate download URL',
                    'details': str(e),
                    'status_code': status.HTTP_500_INTERNAL_SERVER_ERROR,
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class AnalyzeEmailView(APIView):
    """
    Asynchronous analysis of an email using AI Orchestrator.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        email_id = request.data.get('email_id')
        if not email_id:
            return Response({"error": "email_id is required"}, status=400)
        try:
            email = Email.objects.get(id=email_id)
        except Email.DoesNotExist:
            return Response({"error": "Email not found"}, status=404)
        from .tasks import analyze_email_async
        from ai_orchestrator.models import AIAuditLog, AIPersonality, AISkill

        existing_run = AIAuditLog.objects.filter(
            source_type='email',
            source_id=str(email.id),
            status__in=['PENDING', 'PROCESSING'],
        ).order_by('-created_at').first()
        if existing_run:
            return Response({
                "task_id": existing_run.celery_task_id,
                "audit_log_id": str(existing_run.id),
                "status": "processing",
                "message": "An email analysis is already running for this signal.",
            }, status=status.HTTP_200_OK)

        personality = AIPersonality.objects.filter(is_default=True).first()
        skill = AISkill.objects.filter(name='deal_extraction').first()
        default_model = personality.text_model_name if personality else 'qwen3.5:latest'
        audit_log = AIAuditLog.objects.create(
            source_type='email',
            source_id=str(email.id),
            context_label=f"Email: {email.subject}",
            personality=personality,
            skill=skill,
            status='PENDING',
            is_success=False,
            model_used=default_model,
            system_prompt="Queued forensic email analysis...",
            user_prompt=f"Analyzing email signal: {email.subject}",
            source_metadata={
                "email_id": str(email.id),
                "subject": email.subject,
                "email_account": email.email_account.email if email.email_account else None,
                "has_attachments": bool(email.has_attachments),
                "attachment_count": len(email.attachments if isinstance(email.attachments, list) else []),
            },
        )
        task = analyze_email_async.apply_async(
            kwargs={"email_id": str(email.id), "audit_log_id": str(audit_log.id)},
            queue='low_priority'
        )
        audit_log.celery_task_id = task.id
        audit_log.save(update_fields=['celery_task_id'])
        return Response({
            "task_id": task.id,
            "audit_log_id": str(audit_log.id),
            "status": "queued",
            "message": "Email analysis queued.",
        }, status=status.HTTP_200_OK)

class AnalyzeOneDriveFileView(APIView):
    """
    Analyze a file from the DMS shared drive using the AI Orchestrator.
    Downloads the file, extracts text, and sends it to the LLM.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Analyze a OneDrive file with AI",
        description="Downloads a file from the DMS shared drive, extracts text, and analyzes it with AI.",
        tags=["OneDrive"],
        parameters=[
            OpenApiParameter(name='file_id', type=OpenApiTypes.STR, location=OpenApiParameter.QUERY, required=True,
                             description='The item ID of the file to analyze'),
            OpenApiParameter(name='filename', type=OpenApiTypes.STR, location=OpenApiParameter.QUERY, required=True,
                             description='Original filename (used for text extraction)'),
            OpenApiParameter(name='deal_id', type=OpenApiTypes.STR, location=OpenApiParameter.QUERY, required=False,
                             description='The Deal ID to link and vectorize this file to'),
        ],
        responses={200: OpenApiTypes.OBJECT},
    )
    def post(self, request):
        """Download a file from the DMS drive, extract text, and run AI analysis."""
        file_id = request.query_params.get('file_id')
        filename = request.query_params.get('filename')
        deal_id = request.query_params.get('deal_id')

        if not all([file_id, filename]):
            missing = [p for p in ('file_id', 'filename') if not request.query_params.get(p)]
            return Response(
                {
                    'error': 'Validation failed',
                    'details': {p: ['This query parameter is required'] for p in missing},
                    'status_code': status.HTTP_400_BAD_REQUEST,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            # 1. Download file bytes from OneDrive
            print(f"\n[ONEDRIVE ANALYSIS] Starting pipeline for file: {filename}")
            graph = GraphAPIService()
            file_content = graph.get_drive_item_content(DMS_USER_EMAIL, file_id)

            if not file_content:
                print(f"[ONEDRIVE ANALYSIS] ERROR: Failed to download file content for {filename}")
                return Response(
                    {
                        'error': 'Download failed',
                        'details': 'Graph API returned empty content for this file',
                        'status_code': status.HTTP_502_BAD_GATEWAY,
                    },
                    status=status.HTTP_502_BAD_GATEWAY,
                )

            # 2. Extract text from the document
            print(f"[ONEDRIVE ANALYSIS] Extracting text from downloaded bytes...")
            doc_processor = DocumentProcessorService()
            extracted_text = doc_processor.extract_text(file_content, filename)

            # 3. Analyze with AI
            print(f"[ONEDRIVE ANALYSIS] Sending {len(extracted_text)} chars to AI Orchestrator...")
            ai_service = AIProcessorService()
            result = ai_service.process_content(
                content=extracted_text,
                skill_name="document_analysis",
                metadata={'filename': filename},
                source_id=file_id,
                source_type="onedrive_file",
            )
            
            # 4. Vectorize for RAG if Deal ID is provided
            if deal_id:
                try:
                    from deals.models import Deal
                    deal = Deal.objects.get(id=deal_id)
                    print(f"[ONEDRIVE ANALYSIS] Vectorizing for Deal: {deal.title}")
                    embed_service = EmbeddingService()
                    embed_service.chunk_and_embed(
                        text=extracted_text,
                        deal=deal,
                        source_type='onedrive',
                        source_id=file_id,
                        metadata={'filename': filename}
                    )
                except Deal.DoesNotExist:
                    print(f"[ONEDRIVE ANALYSIS] Deal {deal_id} not found, skipping vectorization.")
                except Exception as ve:
                    logger.error(f"OneDrive Vectorization failed: {str(ve)}")

            return Response(result, status=status.HTTP_200_OK)

        except ValueError as e:
            # Authentication/permission errors
            logger.error(f"OneDrive authentication error: {e}", exc_info=True)
            return Response(
                {
                    'error': 'Authentication required',
                    'details': str(e),
                    'status_code': status.HTTP_401_UNAUTHORIZED,
                },
                status=status.HTTP_401_UNAUTHORIZED,
            )
        except Exception as e:
            logger.error(f"Error analyzing OneDrive file: {str(e)}", exc_info=True)
            return Response(
                {
                    'error': 'Failed to analyze file',
                    'details': str(e),
                    'status_code': status.HTTP_500_INTERNAL_SERVER_ERROR,
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
