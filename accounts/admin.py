"""
Admin configuration for accounts app.
"""
from django.contrib import admin
from .models import Profile


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'email', 'is_admin', 'is_disabled', 'created_at')
    list_filter = ('is_admin', 'is_disabled', 'created_at')
    search_fields = ('name', 'email')
    readonly_fields = ('id', 'created_at', 'updated_at')
