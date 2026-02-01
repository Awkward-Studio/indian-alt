import uuid
from django.db import models
from django.contrib.auth.models import User


class Profile(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name='profile',
        null=True,
        blank=True,
        help_text='Link to Django User (optional, for compatibility)'
    )
    name = models.TextField(blank=True, null=True)
    email = models.EmailField()
    image_url = models.URLField(blank=True, null=True)
    is_admin = models.BooleanField(default=False)
    initials = models.CharField(max_length=10, blank=True, null=True)
    is_disabled = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'profile'
        ordering = ['name', 'email']
        verbose_name = 'Profile'
        verbose_name_plural = 'Profiles'
        indexes = [
            models.Index(fields=['email']),
            models.Index(fields=['is_admin']),
        ]

    def __str__(self):
        return self.name or self.email or f'Profile {self.id}'

    def save(self, *args, **kwargs):
        # Auto-generate initials from name if not provided (e.g., "John Doe" -> "JD")
        try:
            if not self.initials and self.name:
                parts = self.name.split()
                if len(parts) >= 2:
                    self.initials = (parts[0][0] + parts[1][0]).upper()
                elif len(parts) == 1:
                    self.initials = parts[0][0].upper()
        except Exception:
            # If initials generation fails, continue without it
            pass
        super().save(*args, **kwargs)
