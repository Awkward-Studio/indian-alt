"""
Email models for storing email data from Microsoft Graph API.
"""
import uuid
from django.db import models
from django.contrib.postgres.fields import ArrayField
from django.core.validators import EmailValidator


class EmailAccount(models.Model):
    """
    Tracks which email addresses to monitor for email reading.
    All emails in the same Microsoft 365 tenant can be accessed with one app registration.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(
        unique=True,
        validators=[EmailValidator()],
        help_text='Email address to monitor (e.g., dms-demo@india-alt.com)'
    )
    is_active = models.BooleanField(
        default=True,
        help_text='Whether to actively monitor this email account'
    )
    last_synced = models.DateTimeField(
        null=True,
        blank=True,
        help_text='Last successful sync timestamp'
    )
    sync_error = models.TextField(
        blank=True,
        null=True,
        help_text='Last error message if sync failed'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'email_account'
        ordering = ['email']
        verbose_name = 'Email Account'
        verbose_name_plural = 'Email Accounts'
        indexes = [
            models.Index(fields=['email']),
            models.Index(fields=['is_active']),
            models.Index(fields=['-last_synced']),
        ]

    def __str__(self):
        return self.email


class EmailImportance(models.TextChoices):
    """Email importance levels from Microsoft Graph API."""
    LOW = 'low', 'Low'
    NORMAL = 'normal', 'Normal'
    HIGH = 'high', 'High'


class Email(models.Model):
    """
    Stores email data retrieved from Microsoft Graph API.
    Includes comprehensive metadata for future AI processing.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email_account = models.ForeignKey(
        EmailAccount,
        on_delete=models.CASCADE,
        related_name='emails',
        help_text='Email account this email belongs to'
    )
    
    # Graph API identifiers
    graph_id = models.CharField(
        max_length=255,
        unique=True,
        db_index=True,
        help_text='Microsoft Graph API message ID'
    )
    internet_message_id = models.CharField(
        max_length=500,
        blank=True,
        null=True,
        db_index=True,
        help_text='Internet Message ID (unique email identifier)'
    )
    
    # Core email fields
    subject = models.TextField(blank=True, null=True)
    from_email = models.EmailField(blank=True, null=True)
    to_emails = ArrayField(
        models.EmailField(),
        default=list,
        blank=True,
        help_text='Array of recipient email addresses'
    )
    cc_emails = ArrayField(
        models.EmailField(),
        default=list,
        blank=True,
        help_text='Array of CC email addresses'
    )
    bcc_emails = ArrayField(
        models.EmailField(),
        default=list,
        blank=True,
        help_text='Array of BCC email addresses'
    )
    
    # Email body
    body_text = models.TextField(blank=True, null=True, help_text='Plain text email body')
    body_html = models.TextField(blank=True, null=True, help_text='HTML email body')
    body_preview = models.TextField(
        blank=True,
        null=True,
        help_text='Email body preview from Graph API'
    )
    
    # Rich metadata from Graph API
    date_received = models.DateTimeField(null=True, blank=True)
    date_sent = models.DateTimeField(null=True, blank=True)
    created_date_time = models.DateTimeField(null=True, blank=True)
    last_modified_date_time = models.DateTimeField(null=True, blank=True)
    
    importance = models.CharField(
        max_length=10,
        choices=EmailImportance.choices,
        default=EmailImportance.NORMAL,
        blank=True,
        null=True
    )
    is_read = models.BooleanField(default=False)
    is_read_receipt_requested = models.BooleanField(default=False)
    
    # Conversation threading
    conversation_id = models.CharField(max_length=255, blank=True, null=True, db_index=True)
    conversation_index = models.TextField(blank=True, null=True)
    
    # Categories and flags
    categories = ArrayField(
        models.CharField(max_length=100),
        default=list,
        blank=True,
        help_text='Array of email categories'
    )
    flag = models.JSONField(
        default=dict,
        blank=True,
        null=True,
        help_text='Follow-up flag status from Graph API'
    )
    
    # Attachments and links
    has_attachments = models.BooleanField(default=False)
    web_link = models.URLField(blank=True, null=True, help_text='Outlook web link')
    
    # Extended metadata
    graph_metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text='Additional Graph API fields stored as JSON'
    )
    
    # Attachments metadata
    attachments = models.JSONField(
        default=list,
        blank=True,
        help_text='Attachment metadata from Graph API (filename, content_type, size, etc.)'
    )
    
    # Status for AI processing
    is_processed = models.BooleanField(
        default=False,
        help_text='Whether email has been processed by AI'
    )
    processed_at = models.DateTimeField(null=True, blank=True)
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'email'
        ordering = ['-date_received', '-created_at']
        verbose_name = 'Email'
        verbose_name_plural = 'Emails'
        indexes = [
            models.Index(fields=['email_account']),
            models.Index(fields=['graph_id']),
            models.Index(fields=['internet_message_id']),
            models.Index(fields=['-date_received']),
            models.Index(fields=['from_email']),
            models.Index(fields=['conversation_id']),
            models.Index(fields=['is_processed']),
            models.Index(fields=['is_read']),
        ]

    def __str__(self):
        return f"{self.subject or 'No Subject'} - {self.from_email or 'Unknown'}"
