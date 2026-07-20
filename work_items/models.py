import uuid

from django.db import models


class TaskStatus(models.TextChoices):
    TODO = "todo", "To Do"
    IN_PROGRESS = "in_progress", "In Progress"
    BLOCKED = "blocked", "Blocked"
    DONE = "done", "Done"


class TaskPriority(models.TextChoices):
    LOW = "low", "Low"
    MEDIUM = "medium", "Medium"
    HIGH = "high", "High"


class Task(models.Model):
    class Origin(models.TextChoices):
        MANUAL = "manual", "Manual"
        ANALYSIS = "analysis", "Analysis"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    deal = models.ForeignKey("deals.Deal", on_delete=models.CASCADE, related_name="tasks")
    title = models.TextField()
    description = models.TextField(blank=True, default="")
    status = models.CharField(max_length=20, choices=TaskStatus.choices, default=TaskStatus.TODO)
    priority = models.CharField(max_length=20, choices=TaskPriority.choices, default=TaskPriority.MEDIUM)
    due_date = models.DateField(null=True, blank=True)
    assignee = models.ForeignKey(
        "accounts.Profile", on_delete=models.SET_NULL, null=True, blank=True, related_name="assigned_tasks"
    )
    created_by = models.ForeignKey(
        "accounts.Profile", on_delete=models.SET_NULL, null=True, blank=True, related_name="created_tasks"
    )
    origin = models.CharField(max_length=20, choices=Origin.choices, default=Origin.MANUAL)
    fingerprint = models.CharField(max_length=64, blank=True, default="", db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "task"
        ordering = ["due_date", "-created_at"]
        indexes = [
            models.Index(fields=["deal", "status"], name="task_deal_id_status_idx"),
            models.Index(fields=["assignee", "status"], name="task_assignee_status_idx"),
            models.Index(fields=["status", "due_date"], name="task_status_due_idx"),
        ]

    def __str__(self):
        return f"{self.deal}: {self.title[:80]}"


class TaskSuggestionState(models.TextChoices):
    PENDING = "pending", "Pending"
    ACCEPTED = "accepted", "Accepted"
    DISMISSED = "dismissed", "Dismissed"
    SUPERSEDED = "superseded", "Superseded"


class TaskSuggestion(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    deal = models.ForeignKey("deals.Deal", on_delete=models.CASCADE, related_name="task_suggestions")
    analysis = models.ForeignKey(
        "deals.DealAnalysis", on_delete=models.CASCADE, null=True, blank=True, related_name="task_suggestions"
    )
    task = models.ForeignKey(Task, on_delete=models.SET_NULL, null=True, blank=True, related_name="source_suggestions")
    analysis_version = models.IntegerField(null=True, blank=True)
    report_hash = models.CharField(max_length=64)
    fingerprint = models.CharField(max_length=64)
    category = models.TextField(blank=True, default="")
    title = models.TextField()
    source_section = models.TextField(blank=True, default="")
    source_table_kind = models.CharField(max_length=40, blank=True, default="")
    source_owner = models.TextField(blank=True, default="")
    source_assignee = models.TextField(blank=True, default="")
    source_status = models.TextField(blank=True, default="")
    source_priority = models.TextField(blank=True, default="")
    source_references = models.JSONField(default=list, blank=True)
    state = models.CharField(
        max_length=20, choices=TaskSuggestionState.choices, default=TaskSuggestionState.PENDING
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "task_suggestion"
        ordering = ["deal__title", "source_section", "created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["deal", "report_hash", "fingerprint"], name="unique_task_suggestion_revision"
            )
        ]
        indexes = [
            models.Index(fields=["deal", "state"], name="task_sugg_deal_state_idx"),
            models.Index(fields=["analysis", "state"], name="task_sugg_analysis_state_idx"),
            models.Index(fields=["report_hash", "state"], name="task_sugg_report_state_idx"),
        ]

    def __str__(self):
        return f"{self.deal}: {self.title[:80]} ({self.state})"
