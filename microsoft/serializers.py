"""
Serializers for email and OneDrive models.
"""
from rest_framework import serializers
from .models import EmailAccount, Email
from .services.granola_meeting_ingestion import GranolaMeetingEmailIngestionService
from deals.services.report_status import report_status_for_deal


class EmailAccountSerializer(serializers.ModelSerializer):
    """Full serializer for EmailAccount (detail view)."""
    
    email_count = serializers.IntegerField(
        source='emails.count',
        read_only=True,
        help_text='Number of emails stored for this account'
    )
    
    class Meta:
        model = EmailAccount
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at')


class EmailAccountListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for EmailAccount (list view)."""
    
    email_count = serializers.IntegerField(
        source='emails.count',
        read_only=True
    )
    
    class Meta:
        model = EmailAccount
        fields = (
            'id', 'email', 'is_active', 'last_synced',
            'sync_error', 'email_count', 'created_at'
        )
        read_only_fields = ('id', 'created_at')


class EmailSerializer(serializers.ModelSerializer):
    """Full serializer for Email (detail view)."""
    
    email_account_email = serializers.EmailField(
        source='email_account.email',
        read_only=True
    )
    is_meeting_note_email = serializers.SerializerMethodField()
    sanitizer_version = serializers.IntegerField(
        source='body_html_sanitizer_version',
        read_only=True,
    )

    def get_is_meeting_note_email(self, obj):
        return GranolaMeetingEmailIngestionService.is_meeting_note_email(obj)
    
    class Meta:
        model = Email
        fields = '__all__'
        read_only_fields = (
            'id', 'created_at', 'updated_at', 'graph_id',
            'internet_message_id'
        )


class EmailListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for Email (list view)."""
    
    email_account_email = serializers.EmailField(
        source='email_account.email',
        read_only=True
    )
    deal_title = serializers.CharField(source='deal.title', read_only=True, default=None)
    is_meeting_note_email = serializers.SerializerMethodField()
    latest_ingestion = serializers.SerializerMethodField()
    sanitizer_version = serializers.IntegerField(
        source='body_html_sanitizer_version',
        read_only=True,
    )
    deal_report_status = serializers.SerializerMethodField()

    def get_is_meeting_note_email(self, obj):
        runs = getattr(obj, 'prefetched_ingestion_runs', None)
        run = runs[0] if runs else (obj.ingestion_runs.first() if hasattr(obj, 'ingestion_runs') else None)
        if run and run.classification and run.classification.get('type'):
            return run.classification.get('type').upper() == 'MEETING_NOTE'
        return GranolaMeetingEmailIngestionService.is_meeting_note_email(obj)

    def get_latest_ingestion(self, obj):
        runs = getattr(obj, 'prefetched_ingestion_runs', None)
        run = runs[0] if runs else (obj.ingestion_runs.first() if hasattr(obj, 'ingestion_runs') else None)
        if not run:
            is_meeting = GranolaMeetingEmailIngestionService.is_meeting_note_email(obj)
            return {
                'run_id': None,
                'status': 'completed' if obj.is_processed or obj.deal_id else 'pending',
                'revision': 0,
                'classification_type': 'MEETING_NOTE' if is_meeting else ('NORMAL_EMAIL' if obj.is_processed else None),
                'classification_confidence': 1.0 if (is_meeting or obj.deal_id) else None,
                'matched_deal_id': str(obj.deal_id) if obj.deal_id else None,
                'matched_deal_title': obj.deal.title if obj.deal else None,
                'deal_match_confidence': 1.0 if obj.deal_id else None,
                'deal_match_status': 'confirmed' if obj.deal_id else 'unmatched',
                'stages': {},
                'error': '',
            }
        match = run.match or {}
        classification = run.classification or {}
        return {
            'run_id': str(run.id),
            'status': run.status,
            'revision': run.revision,
            'classification_type': classification.get('type'),
            'classification_confidence': classification.get('confidence'),
            'matched_deal_id': match.get('deal_id') or match.get('suggested_deal_id') or (str(obj.deal_id) if obj.deal_id else None),
            'matched_deal_title': match.get('title') or (obj.deal.title if obj.deal else None),
            'deal_match_confidence': match.get('confidence'),
            'deal_match_status': match.get('status'),
            'stages': run.stages or {},
            'error': run.error or '',
        }

    def get_deal_report_status(self, obj):
        if not obj.deal_id or not getattr(obj, 'deal', None):
            return {
                'state': 'not_started', 'analysis_id': None, 'version': None,
                'generated_at': None, 'audit_log_id': None, 'error': None,
            }
        # An email page commonly contains several messages linked to the same
        # deal. Reuse the status within one serializer invocation so the new
        # state contract does not issue the same audit/analysis queries once
        # per email row.
        cache = self.context.setdefault('_deal_report_status_cache', {})
        key = str(obj.deal_id)
        if key not in cache:
            cache[key] = report_status_for_deal(obj.deal)
        return cache[key]
    
    class Meta:
        model = Email
        fields = (
            'id', 'email_account', 'email_account_email', 'subject',
            'from_email', 'to_emails', 'cc_emails', 'bcc_emails',
            'body_text', 'body_html', 'sanitizer_version', 'date_received', 'date_sent',
            'importance', 'is_read', 'has_attachments', 'body_preview', 
            'attachments', 'created_at', 'is_processed', 'is_indexed', 'deal_id',
            'deal_title', 'is_meeting_note_email', 'latest_ingestion', 'deal_report_status'
        )
        read_only_fields = ('id', 'created_at')


class EmailFetchSerializer(serializers.Serializer):
    """Serializer for email fetch endpoint response."""
    
    success = serializers.BooleanField()
    total_accounts = serializers.IntegerField(required=False)
    successful_accounts = serializers.IntegerField(required=False)
    failed_accounts = serializers.IntegerField(required=False)
    total_emails = serializers.IntegerField(required=False)
    count = serializers.IntegerField(required=False)
    new_count = serializers.IntegerField(required=False)
    updated_count = serializers.IntegerField(required=False)
    emails = EmailListSerializer(many=True, required=False)
    errors = serializers.ListField(
        child=serializers.CharField(),
        required=False
    )
    account_results = serializers.DictField(required=False)


# ------------------------------------------------------------------ #
#                     OneDrive Serializers                            #
# ------------------------------------------------------------------ #

class DriveItemFileInfoSerializer(serializers.Serializer):
    """Metadata present when the item is a file."""
    mimeType = serializers.CharField(
        help_text='MIME type of the file (e.g. application/pdf)',
        required=False,
    )


class DriveItemFolderInfoSerializer(serializers.Serializer):
    """Metadata present when the item is a folder."""
    childCount = serializers.IntegerField(
        help_text='Number of immediate children in the folder',
        required=False,
    )


class ParentReferenceSerializer(serializers.Serializer):
    """Reference to the parent folder of the item."""
    driveId = serializers.CharField(help_text='Drive ID', required=False)
    driveType = serializers.CharField(
        help_text='Type of drive (personal, business, documentLibrary)',
        required=False,
    )
    id = serializers.CharField(help_text='Parent item ID', required=False)
    path = serializers.CharField(help_text='Path of the parent', required=False)


class DriveItemSerializer(serializers.Serializer):
    """
    Represents a single OneDrive item (file or folder).
    Mirrors the relevant fields from the Microsoft Graph driveItem resource.
    """
    id = serializers.CharField(help_text='Unique identifier of the drive item')
    name = serializers.CharField(help_text='Name of the item (filename or folder name)')
    size = serializers.IntegerField(
        help_text='Size of the item in bytes',
        required=False,
    )
    webUrl = serializers.URLField(
        help_text='URL to open the item in the browser',
        required=False,
    )
    createdDateTime = serializers.DateTimeField(
        help_text='Date and time the item was created',
        required=False,
    )
    lastModifiedDateTime = serializers.DateTimeField(
        help_text='Date and time the item was last modified',
        required=False,
    )
    file = DriveItemFileInfoSerializer(
        help_text='File metadata (present only for files)',
        required=False,
        allow_null=True,
    )
    folder = DriveItemFolderInfoSerializer(
        help_text='Folder metadata (present only for folders)',
        required=False,
        allow_null=True,
    )
    parentReference = ParentReferenceSerializer(
        help_text='Reference to the parent item',
        required=False,
    )
    item_type = serializers.SerializerMethodField(
        help_text='Convenience field: "file" or "folder"',
    )

    def get_item_type(self, obj) -> str:
        if isinstance(obj, dict):
            if obj.get('folder') is not None:
                return 'folder'
            return 'file'
        return 'file'


class OneDriveListResponseSerializer(serializers.Serializer):
    """Top-level response for the list OneDrive files/folders endpoint."""
    count = serializers.IntegerField(help_text='Number of items returned')
    items = DriveItemSerializer(many=True, help_text='List of drive items')
    next_skip = serializers.IntegerField(
        help_text='Value to pass as skip parameter for the next page (null if no more pages)',
        required=False,
        allow_null=True,
    )
