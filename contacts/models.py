import uuid
from django.db import models
from banks.models import Bank


class Contact(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.TextField(blank=True, null=True)
    email = models.EmailField(blank=True, null=True)
    designation = models.TextField(blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    bank = models.ForeignKey(
        Bank,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='contacts',
        db_column='bank_id'
    )
    location = models.TextField(blank=True, null=True)
    # Array of profile UUIDs - stored as JSON list for SQLite/Postgres compatibility
    responsibility = models.JSONField(
        default=list,
        blank=True,
        help_text='Array of profile UUIDs responsible for this contact'
    )
    phone = models.TextField(blank=True, null=True)
    # Contacts should have at least one sector coverage area (enforced at app level)
    sector_coverage = models.JSONField(
        default=list,
        blank=True,
        help_text='Array of sector coverage areas'
    )
    rank = models.TextField(blank=True, null=True)

    class Meta:
        db_table = 'contact'
        ordering = ['name', 'created_at']
        verbose_name = 'Contact'
        verbose_name_plural = 'Contacts'

    def __str__(self):
        return self.name or f'Contact {self.id}'
