from django.utils import timezone
from rest_framework import serializers

from accounts.models import Profile
from .models import Task, TaskStatus, TaskSuggestion


class CompactProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = Profile
        fields = ("id", "name", "email", "initials", "image_url")


class TaskSerializer(serializers.ModelSerializer):
    assignee = CompactProfileSerializer(read_only=True)
    assignee_id = serializers.PrimaryKeyRelatedField(
        source="assignee", queryset=Profile.objects.filter(is_disabled=False), allow_null=True, required=False
    )
    created_by = CompactProfileSerializer(read_only=True)
    deal_title = serializers.CharField(source="deal.title", read_only=True)
    source_sections = serializers.SerializerMethodField()

    class Meta:
        model = Task
        fields = (
            "id", "deal", "deal_title", "title", "description", "status", "priority", "due_date",
            "assignee", "assignee_id", "created_by", "origin", "fingerprint", "source_sections",
            "completed_at", "created_at", "updated_at",
        )
        read_only_fields = ("id", "created_by", "origin", "fingerprint", "completed_at", "created_at", "updated_at")

    def get_source_sections(self, obj):
        return list(obj.source_suggestions.values_list("source_section", flat=True).distinct())

    def validate_title(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Task title cannot be empty.")
        return value

    def _apply_completion(self, instance, status_value):
        if status_value == TaskStatus.DONE and not instance.completed_at:
            instance.completed_at = timezone.now()
        elif status_value != TaskStatus.DONE:
            instance.completed_at = None

    def create(self, validated_data):
        task = Task(**validated_data)
        self._apply_completion(task, task.status)
        task.save()
        return task

    def update(self, instance, validated_data):
        for key, value in validated_data.items():
            setattr(instance, key, value)
        self._apply_completion(instance, instance.status)
        instance.save()
        return instance


class TaskSuggestionSerializer(serializers.ModelSerializer):
    deal_title = serializers.CharField(source="deal.title", read_only=True)
    linked_task_id = serializers.UUIDField(source="task_id", read_only=True)
    task_title = serializers.SerializerMethodField()
    description = serializers.CharField(source="title", read_only=True)

    def get_task_title(self, obj):
        from .services import concise_task_title
        return concise_task_title(obj)

    class Meta:
        model = TaskSuggestion
        fields = (
            "id", "deal", "deal_title", "analysis", "analysis_version", "task", "linked_task_id",
            "category", "title", "task_title", "description", "source_section", "source_table_kind", "source_owner",
            "source_assignee", "source_status", "source_priority", "source_references", "state",
            "created_at", "updated_at",
        )
        read_only_fields = fields


class TaskSummarySerializer(serializers.Serializer):
    my_open = serializers.IntegerField()
    open = serializers.IntegerField()
    overdue = serializers.IntegerField()
    unassigned = serializers.IntegerField()
    pending_suggestions = serializers.IntegerField()
