"""
Admin configuration for deals app.
"""
from django.contrib import admin
from .models import Deal


@admin.register(Deal)
class DealAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'title', 'bank', 'deal_status', 'priority', 'fund', 'created_at',
        'is_female_led', 'management_meeting'
    )
    list_filter = ('deal_status', 'priority', 'fund', 'is_female_led', 'management_meeting', 'created_at')
    search_fields = ('title', 'deal_summary', 'industry', 'sector')
    readonly_fields = ('id', 'created_at')
