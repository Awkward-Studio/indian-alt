"""
Admin configuration for contacts app.
"""
from django.contrib import admin
from .models import Contact


@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'email', 'bank', 'location', 'created_at')
    list_filter = ('bank', 'created_at')
    search_fields = ('name', 'email', 'designation', 'location')
    readonly_fields = ('id', 'created_at')
