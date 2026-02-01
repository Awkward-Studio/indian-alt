"""
Admin configuration for core app (Version only).
"""
from django.contrib import admin
from .models import Version


@admin.register(Version)
class VersionAdmin(admin.ModelAdmin):
    list_display = ('id', 'item_id', 'type', 'user_id', 'created_at')
    list_filter = ('type', 'created_at')
    search_fields = ('item_id', 'search')
    readonly_fields = ('id', 'created_at')
