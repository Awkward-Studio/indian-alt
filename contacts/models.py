import uuid
from django.conf import settings
from django.db import models
from banks.models import Bank


class Contact(models.Model):
    class ContactType(models.TextChoices):
        BANKER = 'BANKER', 'Banker'
        CONSULTANT = 'CONSULTANT', 'Consultant'
        INVESTOR = 'INVESTOR', 'Investor'
        OTHER = 'OTHER', 'Other'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.TextField(blank=True, null=True)
    email = models.EmailField(blank=True, null=True)
    designation = models.TextField(blank=True, null=True)
    contact_type = models.CharField(
        max_length=20,
        choices=ContactType.choices,
        default=ContactType.OTHER,
        db_index=True,
    )
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
    linkedin_url = models.URLField(blank=True, null=True)
    twitter_handle = models.TextField(blank=True, null=True)
    source_count = models.IntegerField(default=0, help_text='Total deals sourced from this contact')

    # Legacy Banker Database Fields
    ranking = models.TextField(blank=True, null=True, help_text='Legacy Ranking')
    primary_coverage_person = models.TextField(blank=True, null=True, help_text='Person Covering - Primary')
    secondary_coverage_person = models.TextField(blank=True, null=True, help_text='Person Covering - Secondary')
    total_deals_legacy = models.IntegerField(default=0, help_text='Total Deals from Legacy DB')
    pipeline = models.TextField(blank=True, null=True, help_text='Legacy Pipeline/Status')
    follow_ups = models.TextField(blank=True, null=True, help_text='Legacy Follow Ups')
    last_meeting_date = models.TextField(blank=True, null=True, help_text='Legacy Last Meeting / Call Date (Stored as text)')

    class Meta:
        db_table = 'contact'
        ordering = ['name', 'created_at']
        verbose_name = 'Contact'
        verbose_name_plural = 'Contacts'

    def __str__(self):
        return self.name or f'Contact {self.id}'


class WorkplaceVerificationSuggestion(models.Model):
    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending review'
        ACCEPTED = 'ACCEPTED', 'Accepted'
        REJECTED = 'REJECTED', 'Rejected'
        SUPERSEDED = 'SUPERSEDED', 'Superseded'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    contact = models.ForeignKey(
        Contact,
        on_delete=models.CASCADE,
        related_name='workplace_verifications',
    )
    old_bank_name = models.TextField(blank=True, default='')
    old_designation = models.TextField(blank=True, default='')
    proposed_bank_name = models.TextField(blank=True, default='')
    proposed_designation = models.TextField(blank=True, default='')
    source_url = models.URLField(max_length=1000)
    source_title = models.TextField(blank=True, default='')
    source_snippet = models.TextField(blank=True, default='')
    source_domain = models.CharField(max_length=255, blank=True, default='')
    search_query = models.TextField()
    confidence = models.FloatField(default=0)
    retrieved_at = models.DateTimeField()
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    accepted_fields = models.JSONField(default=list, blank=True)
    reviewer_comment = models.TextField(blank=True, default='')
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='requested_workplace_verifications',
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reviewed_workplace_verifications',
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    audit_log = models.ForeignKey(
        'ai_orchestrator.AIAuditLog',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='workplace_verifications',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(
                fields=['contact', 'status', '-created_at'],
                name='contacts_wo_contact_32d9fc_idx',
            ),
        ]

    def __str__(self):
        return f'{self.contact} workplace verification ({self.status})'


class ContactInteraction(models.Model):
    class Kind(models.TextChoices):
        MEETING = 'MEETING', 'Meeting'
        CALL = 'CALL', 'Call'
        EMAIL = 'EMAIL', 'Email'
        NOTE = 'NOTE', 'Note'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name='interactions')
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.NOTE)
    occurred_at = models.DateTimeField()
    notes = models.TextField(blank=True, default='')
    meeting_note = models.ForeignKey('meetings.MeetingNote', on_delete=models.CASCADE, null=True, blank=True, related_name='contact_interactions')
    deal = models.ForeignKey('deals.Deal', on_delete=models.SET_NULL, null=True, blank=True, related_name='contact_interactions')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-occurred_at', '-created_at']
        constraints = [models.UniqueConstraint(fields=['contact', 'meeting_note'], name='unique_contact_meeting_interaction')]


class ContactCardExtraction(models.Model):
    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending review'
        COMPLETED = 'COMPLETED', 'Completed'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='contact_card_extractions')
    file_name = models.TextField()
    file_size = models.PositiveIntegerField(default=0)
    raw_text = models.TextField(blank=True, default='')
    extracted_data = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    matched_contact = models.ForeignKey(Contact, on_delete=models.SET_NULL, null=True, blank=True, related_name='card_extractions')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
