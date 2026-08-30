import uuid
from django.conf import settings
from django.db import models
from contacts.models import Contact


class MeetingNoteSource(models.TextChoices):
    MANUAL = 'manual', 'Manual'
    EMAIL = 'email', 'Email'


class Meeting(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    notes = models.TextField()
    location = models.TextField(blank=True, null=True)
    pipeline = models.TextField(blank=True, null=True)
    follow_ups = models.TextField(blank=True, null=True)
    followup_completed = models.BooleanField(default=False)
    # Using through models allows for future extension (e.g., adding timestamps, roles)
    contacts = models.ManyToManyField(
        Contact,
        through='MeetingContact',
        related_name='meetings'
    )
    profiles = models.ManyToManyField(
        'accounts.Profile',
        through='MeetingProfile',
        related_name='meetings'
    )

    class Meta:
        db_table = 'meeting'
        ordering = ['-created_at']
        verbose_name = 'Meeting'
        verbose_name_plural = 'Meetings'

    def __str__(self):
        return f'Meeting {self.id} - {self.created_at.date()}'


class MeetingNote(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=255, blank=True, default='')
    body = models.TextField(help_text='Raw meeting note text from manual entry or email ingestion')
    summary = models.TextField(blank=True, default='')
    meeting_at = models.DateTimeField(blank=True, null=True)
    location = models.TextField(blank=True, null=True)
    attendees = models.TextField(blank=True, default='', help_text='Free-form attendee list captured from notes or email')
    action_items = models.TextField(blank=True, default='')
    decisions = models.TextField(blank=True, default='')
    source = models.CharField(
        max_length=20,
        choices=MeetingNoteSource.choices,
        default=MeetingNoteSource.MANUAL,
    )
    source_email = models.ForeignKey(
        'microsoft.Email',
        on_delete=models.SET_NULL,
        related_name='meeting_notes',
        null=True,
        blank=True,
        help_text='Email this meeting note was ingested from, when applicable',
    )
    deals = models.ManyToManyField(
        'deals.Deal',
        related_name='meeting_notes',
        blank=True,
        help_text='Deals discussed in this meeting note',
    )
    contacts = models.ManyToManyField(
        Contact,
        related_name='meeting_notes',
        blank=True,
        help_text='Contacts who attended or were discussed in this meeting note',
    )
    created_by = models.ForeignKey(
        'accounts.Profile',
        on_delete=models.SET_NULL,
        related_name='created_meeting_notes',
        null=True,
        blank=True,
    )
    metadata = models.JSONField(default=dict, blank=True)
    is_indexed = models.BooleanField(default=False)
    chunk_count = models.PositiveIntegerField(default=0)
    embedding_error = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'meeting_note'
        ordering = ['-meeting_at', '-created_at']
        verbose_name = 'Meeting Note'
        verbose_name_plural = 'Meeting Notes'
        indexes = [
            models.Index(fields=['source', '-created_at']),
            models.Index(fields=['meeting_at']),
            models.Index(fields=['is_indexed']),
        ]

    def __str__(self):
        return self.title or f'Meeting Note {self.id}'


class MeetingSignalFlag(models.Model):
    class Kind(models.TextChoices):
        RED = 'RED', 'Red flag'
        GREEN = 'GREEN', 'Green signal'
        QUESTION = 'QUESTION', 'Open question'

    class ReviewStatus(models.TextChoices):
        UNREVIEWED = 'UNREVIEWED', 'Unreviewed'
        CONFIRMED = 'CONFIRMED', 'Confirmed'
        DISMISSED = 'DISMISSED', 'Dismissed'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    deal = models.ForeignKey(
        'deals.Deal',
        on_delete=models.CASCADE,
        related_name='meeting_signal_flags',
    )
    fingerprint = models.CharField(max_length=64)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    title = models.CharField(max_length=500)
    detail = models.TextField()
    confidence = models.CharField(max_length=20, default='medium')
    evidence = models.JSONField(default=list)
    source_note_ids = models.JSONField(default=list)
    first_audit_log = models.ForeignKey(
        'ai_orchestrator.AIAuditLog',
        on_delete=models.SET_NULL,
        null=True,
        related_name='first_detected_meeting_signals',
    )
    latest_audit_log = models.ForeignKey(
        'ai_orchestrator.AIAuditLog',
        on_delete=models.SET_NULL,
        null=True,
        related_name='latest_detected_meeting_signals',
    )
    detection_count = models.PositiveIntegerField(default=1)
    review_status = models.CharField(
        max_length=20,
        choices=ReviewStatus.choices,
        default=ReviewStatus.UNREVIEWED,
        db_index=True,
    )
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reviewed_meeting_signals',
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_comment = models.TextField(blank=True)
    first_detected_at = models.DateTimeField(auto_now_add=True)
    last_detected_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'meeting_signal_flag'
        ordering = ['-last_detected_at', 'kind', 'title']
        constraints = [
            models.UniqueConstraint(
                fields=['deal', 'fingerprint'],
                name='unique_meeting_signal_fingerprint_per_deal',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(reviewer__isnull=True, reviewed_at__isnull=True)
                    | models.Q(reviewer__isnull=False, reviewed_at__isnull=False)
                ),
                name='meeting_signal_reviewer_fields_paired',
            ),
        ]
        indexes = [
            models.Index(fields=['deal', 'review_status']),
            models.Index(fields=['deal', 'kind']),
        ]


class MeetingContact(models.Model):
    id = models.BigAutoField(primary_key=True)
    meeting = models.ForeignKey(
        Meeting,
        on_delete=models.CASCADE,
        related_name='meeting_contacts',
        db_column='meeting_id'
    )
    contact = models.ForeignKey(
        Contact,
        on_delete=models.CASCADE,
        related_name='meeting_contacts',
        db_column='contact_id'
    )

    class Meta:
        db_table = 'meeting_contact'
        unique_together = [['meeting', 'contact']]
        verbose_name = 'Meeting Contact'
        verbose_name_plural = 'Meeting Contacts'

    def __str__(self):
        return f'{self.meeting} - {self.contact}'


class MeetingProfile(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    meeting = models.ForeignKey(
        Meeting,
        on_delete=models.CASCADE,
        related_name='meeting_profiles',
        db_column='meeting_id'
    )
    profile = models.ForeignKey(
        'accounts.Profile',
        on_delete=models.CASCADE,
        related_name='meeting_profiles',
        db_column='profile_id'
    )

    class Meta:
        db_table = 'meeting_profile'
        unique_together = [['meeting', 'profile']]
        verbose_name = 'Meeting Profile'
        verbose_name_plural = 'Meeting Profiles'

    def __str__(self):
        return f'{self.meeting} - {self.profile}'
