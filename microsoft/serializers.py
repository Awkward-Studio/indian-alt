"""
Serializers for email and OneDrive models.
"""
from rest_framework import serializers
from .models import EmailAccount, Email


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
    
    class Meta:
        model = Email
        fields = (
            'id', 'email_account', 'email_account_email', 'subject',
            'from_email', 'to_emails', 'cc_emails', 'bcc_emails',
            'body_text', 'body_html', 'date_received', 'date_sent', 
            'importance', 'is_read', 'has_attachments', 'body_preview', 
            'created_at'
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
