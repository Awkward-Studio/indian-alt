"""
Admin configuration for requests app.
"""
from django.contrib import admin
from .models import Request


@admin.register(Request)
class RequestAdmin(admin.ModelAdmin):
    list_display = ('id', 'status', 'created_at')
    list_filter = ('status', 'created_at')
    search_fields = ('logs',)
    readonly_fields = ('id', 'created_at')
