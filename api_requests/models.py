import uuid
from django.db import models


class RequestStatus(models.TextChoices):
    PENDING = 'Pending', 'Pending'
    IN_PROGRESS = 'In Progress', 'In Progress'
    COMPLETED = 'Completed', 'Completed'
    CONFLICT = 'Conflict', 'Conflict'
    HIGH = 'High', 'High'


class Request(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    metadata = models.JSONField(default=dict, blank=True, null=True)
    body = models.JSONField(default=dict, blank=True, null=True)
    attachments = models.JSONField(default=dict, blank=True, null=True)
    status = models.CharField(
        max_length=20,
        choices=RequestStatus.choices,
        default=RequestStatus.PENDING,
        db_column='status'
    )
    logs = models.TextField(blank=True, null=True)

    class Meta:
        db_table = 'request'
        ordering = ['-created_at']
        verbose_name = 'Request'
        verbose_name_plural = 'Requests'

    def __str__(self):
        return f'Request {self.id} - {self.status}'
