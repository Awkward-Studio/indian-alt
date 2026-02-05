"""
Admin interface for email models.
"""
from django.contrib import admin
from .models import EmailAccount, Email


@admin.register(EmailAccount)
class EmailAccountAdmin(admin.ModelAdmin):
    """Admin interface for EmailAccount."""
    list_display = ('email', 'is_active', 'last_synced', 'sync_error', 'created_at')
    list_filter = ('is_active', 'created_at', 'last_synced')
    search_fields = ('email',)
    readonly_fields = ('id', 'created_at', 'updated_at')
    fieldsets = (
        ('Basic Information', {
            'fields': ('id', 'email', 'is_active')
        }),
        ('Sync Status', {
            'fields': ('last_synced', 'sync_error')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at')
        }),
    )


@admin.register(Email)
class EmailAdmin(admin.ModelAdmin):
    """Admin interface for Email."""
    list_display = (
        'subject', 'from_email', 'email_account', 'date_received',
        'is_read', 'is_processed', 'importance'
    )
    list_filter = (
        'email_account', 'is_read', 'is_processed', 'importance',
        'has_attachments', 'date_received'
    )
    search_fields = ('subject', 'from_email', 'body_text', 'body_preview')
    readonly_fields = (
        'id', 'graph_id', 'internet_message_id', 'created_at', 'updated_at'
    )
    date_hierarchy = 'date_received'
    fieldsets = (
        ('Basic Information', {
            'fields': ('id', 'email_account', 'graph_id', 'internet_message_id')
        }),
        ('Email Content', {
            'fields': ('subject', 'from_email', 'to_emails', 'cc_emails', 'bcc_emails')
        }),
        ('Body', {
            'fields': ('body_text', 'body_html', 'body_preview'),
            'classes': ('collapse',)
        }),
        ('Metadata', {
            'fields': (
                'date_received', 'date_sent', 'created_date_time',
                'last_modified_date_time', 'importance', 'is_read',
                'is_read_receipt_requested'
            )
        }),
        ('Threading', {
            'fields': ('conversation_id', 'conversation_index'),
            'classes': ('collapse',)
        }),
        ('Categories & Flags', {
            'fields': ('categories', 'flag'),
            'classes': ('collapse',)
        }),
        ('Attachments', {
            'fields': ('has_attachments', 'attachments', 'web_link')
        }),
        ('Processing Status', {
            'fields': ('is_processed', 'processed_at')
        }),
        ('Extended Metadata', {
            'fields': ('graph_metadata',),
            'classes': ('collapse',)
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at')
        }),
    )
