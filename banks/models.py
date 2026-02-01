import uuid
from django.db import models


class Bank(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'bank'
        ordering = ['name']
        verbose_name = 'Bank'
        verbose_name_plural = 'Banks'

    def __str__(self):
        return self.name or f'Bank {self.id}'
