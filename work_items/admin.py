from django.contrib import admin

from .models import Task, TaskSuggestion


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ("title", "deal", "assignee", "status", "priority", "due_date", "created_at")
    list_filter = ("status", "priority", "origin")
    search_fields = ("title", "deal__title", "assignee__name", "assignee__email")


@admin.register(TaskSuggestion)
class TaskSuggestionAdmin(admin.ModelAdmin):
    list_display = ("title", "deal", "analysis_version", "state", "source_section", "created_at")
    list_filter = ("state", "source_table_kind")
    search_fields = ("title", "deal__title", "source_section")
