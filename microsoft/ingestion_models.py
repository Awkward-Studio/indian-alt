"""Durable, mailbox-scoped source records for email evidence ingestion."""
import uuid

from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.db import models
from django.utils.deconstruct import deconstructible


@deconstructible
class PrivateEmailStorage(FileSystemStorage):
    def __init__(self):
        super().__init__(location=getattr(settings, 'EMAIL_EVIDENCE_ROOT', settings.BASE_DIR / 'private_email_evidence'))

    def url(self, name):
        raise ValueError('Email evidence requires authenticated download.')


class EmailIngestionRun(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.ForeignKey('microsoft.Email', on_delete=models.CASCADE, related_name='ingestion_runs')
    input_version = models.CharField(max_length=64)
    pipeline_version = models.PositiveIntegerField(default=1)
    source = models.JSONField(default=dict)
    classification = models.JSONField(default=dict)
    match = models.JSONField(default=dict)
    stages = models.JSONField(default=dict)
    revision = models.PositiveIntegerField(default=1)
    decision_log = models.JSONField(default=list)
    status = models.CharField(max_length=24, default='pending', db_index=True)
    error = models.TextField(blank=True)
    attempts = models.PositiveIntegerField(default=0)
    lease_token = models.UUIDField(null=True, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['email', 'input_version', 'pipeline_version'], name='email_ingestion_input_unique')]
        ordering = ['-created_at']


class EmailContribution(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email_account = models.ForeignKey('microsoft.EmailAccount', on_delete=models.CASCADE)
    fingerprint = models.CharField(max_length=64)
    text = models.TextField()
    headers = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['email_account', 'fingerprint'], name='email_contribution_unique')]


class EmailPrivateBlob(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email_account = models.ForeignKey('microsoft.EmailAccount', on_delete=models.CASCADE)
    sha256 = models.CharField(max_length=64)
    file = models.FileField(storage=PrivateEmailStorage(), upload_to='attachments', max_length=500)
    size = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['email_account', 'sha256'], name='email_blob_unique')]


class EmailEvidenceLink(models.Model):
    """One canonical output per source, deal and mailbox; occurrences own membership."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email_account = models.ForeignKey('microsoft.EmailAccount', on_delete=models.CASCADE)
    deal = models.ForeignKey('deals.Deal', on_delete=models.CASCADE)
    source_key = models.CharField(max_length=100)
    kind = models.CharField(max_length=24)
    document = models.ForeignKey('deals.DealDocument', null=True, blank=True, on_delete=models.SET_NULL, related_name='email_evidence_links')
    meeting_note = models.ForeignKey('meetings.MeetingNote', null=True, blank=True, on_delete=models.SET_NULL, related_name='email_evidence_links')
    blob = models.ForeignKey(EmailPrivateBlob, null=True, blank=True, on_delete=models.PROTECT)
    provenance = models.JSONField(default=dict)
    index_status = models.CharField(max_length=24, default='pending')
    error = models.TextField(blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['email_account', 'deal', 'source_key', 'kind'], name='email_evidence_output_unique')]


class EmailContributionOccurrence(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(EmailIngestionRun, on_delete=models.CASCADE, related_name='occurrences')
    source_key = models.CharField(max_length=1100)
    position = models.PositiveIntegerField(default=0)
    contribution = models.ForeignKey(EmailContribution, null=True, blank=True, on_delete=models.PROTECT)
    evidence = models.ForeignKey(EmailEvidenceLink, null=True, blank=True, on_delete=models.SET_NULL, related_name='occurrences')
    blob = models.ForeignKey(EmailPrivateBlob, null=True, blank=True, on_delete=models.PROTECT)
    metadata = models.JSONField(default=dict)
    status = models.CharField(max_length=24, default='pending')
    error = models.TextField(blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['run', 'source_key'], name='email_occurrence_unique')]
        ordering = ['position', 'id']


class EmailIngestionBackfill(models.Model):
    """Durable cursor and aggregate counters for bounded historical runs."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.CharField(max_length=120, unique=True)
    filters = models.JSONField(default=dict)
    cursor = models.UUIDField(null=True, blank=True)
    stats = models.JSONField(default=dict)
    completed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
