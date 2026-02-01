"""
Admin configuration for meetings app.
"""
from django.contrib import admin
from .models import Meeting, MeetingContact, MeetingProfile


@admin.register(Meeting)
class MeetingAdmin(admin.ModelAdmin):
    list_display = ('id', 'created_at', 'location', 'followup_completed')
    list_filter = ('followup_completed', 'created_at')
    search_fields = ('notes', 'location', 'pipeline', 'follow_ups')
    readonly_fields = ('id', 'created_at')


@admin.register(MeetingContact)
class MeetingContactAdmin(admin.ModelAdmin):
    list_display = ('id', 'meeting', 'contact')
    list_filter = ('meeting', 'contact')


@admin.register(MeetingProfile)
class MeetingProfileAdmin(admin.ModelAdmin):
    list_display = ('id', 'meeting', 'profile')
    list_filter = ('meeting', 'profile')
