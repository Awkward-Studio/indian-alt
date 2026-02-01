import uuid
from django.db import models
from contacts.models import Contact


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
