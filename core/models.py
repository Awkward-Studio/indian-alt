import uuid
from django.db import models


class Version(models.Model):
    # Audit history table - records are created by database triggers, not Django
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item_id = models.UUIDField(
        help_text='References the object ID (deal or contact)'
    )
    type = models.CharField(
        max_length=20,
        choices=[('deal', 'Deal'), ('contact', 'Contact')],
        help_text='Type of item (deal or contact)'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    # Full snapshot of the item at the time of change
    data = models.JSONField(default=dict, help_text='Full JSON snapshot of the item')
    search = models.TextField(blank=True, null=True, help_text='Searchable text (title/name)')
    user_id = models.UUIDField(
        blank=True,
        null=True,
        help_text='User who made the change (from auth.uid())'
    )

    class Meta:
        db_table = 'version'
        ordering = ['-created_at']
        verbose_name = 'Version'
        verbose_name_plural = 'Versions'
        indexes = [
            models.Index(fields=['item_id', 'type']),
            models.Index(fields=['-created_at']),
        ]

    def __str__(self):
        return f'Version {self.id} - {self.type} - {self.item_id}'
