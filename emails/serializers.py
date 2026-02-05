"""
Serializers for email models.
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
            'from_email', 'date_received', 'date_sent', 'importance',
            'is_read', 'has_attachments', 'body_preview', 'created_at'
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
