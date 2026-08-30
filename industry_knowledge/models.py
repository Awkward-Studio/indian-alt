import uuid

from django.conf import settings
from django.db import models


class KnowledgeDocument(models.Model):
    class Kind(models.TextChoices):
        REPORT = "REPORT", "Industry report"
        TRANSCRIPT = "TRANSCRIPT", "Industry call transcript"

    class Visibility(models.TextChoices):
        INTERNAL = "INTERNAL", "All IA users"
        RESTRICTED = "RESTRICTED", "Publisher and administrators"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    title = models.CharField(max_length=500)
    publisher = models.CharField(max_length=255, blank=True)
    published_at = models.DateField(null=True, blank=True)
    sector = models.CharField(max_length=255, blank=True, db_index=True)
    themes = models.JSONField(default=list, blank=True)
    visibility = models.CharField(max_length=20, choices=Visibility.choices, default=Visibility.INTERNAL)
    confidentiality = models.CharField(max_length=255, blank=True)
    participants = models.TextField(blank=True)
    redaction_note = models.TextField(blank=True)
    deal_document = models.OneToOneField("deals.DealDocument", null=True, blank=True, on_delete=models.CASCADE, related_name="knowledge_publication")
    meeting_note = models.OneToOneField("meetings.MeetingNote", null=True, blank=True, on_delete=models.CASCADE, related_name="knowledge_publication")
    published_by = models.ForeignKey("accounts.Profile", null=True, on_delete=models.SET_NULL, related_name="published_knowledge_documents")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-published_at", "-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(deal_document__isnull=False, meeting_note__isnull=True) | models.Q(deal_document__isnull=True, meeting_note__isnull=False)),
                name="knowledge_document_has_one_source",
            )
        ]
        indexes = [models.Index(fields=["kind", "sector"]), models.Index(fields=["visibility", "-created_at"])]


class IATheme(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120, unique=True)
    description = models.TextField(blank=True)
    keywords = models.JSONField(default=list, blank=True)
    is_active = models.BooleanField(default=True)
    subscribed_by = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True, related_name="industry_theme_subscriptions")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]


class NewsSource(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=160, unique=True)
    feed_url = models.URLField(blank=True)
    homepage_url = models.URLField(blank=True)
    is_active = models.BooleanField(default=False)
    requires_licensed_api = models.BooleanField(default=False)
    last_fetched_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]


class NewsArticle(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.ForeignKey(NewsSource, on_delete=models.CASCADE, related_name="articles")
    title = models.CharField(max_length=600)
    url = models.URLField(unique=True)
    summary = models.TextField(blank=True)
    author = models.CharField(max_length=255, blank=True)
    published_at = models.DateTimeField(null=True, blank=True, db_index=True)
    companies = models.JSONField(default=list, blank=True)
    themes = models.ManyToManyField(IATheme, blank=True, related_name="articles")
    saved_by = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True, related_name="saved_industry_news")
    dismissed_by = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True, related_name="dismissed_industry_news")
    linked_deals = models.ManyToManyField("deals.Deal", blank=True, related_name="industry_news")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-published_at", "-created_at"]
        indexes = [models.Index(fields=["source", "-published_at"])]
