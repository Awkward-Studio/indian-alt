from datetime import timedelta

from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone
from rest_framework import filters, status, viewsets
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

from deals.models import Deal
from .models import Task, TaskActivity, TaskComment, TaskPriority, TaskStatus, TaskSuggestion, TaskSuggestionState
from .permissions import IsActiveProfile
from .serializers import TaskActivitySerializer, TaskCommentSerializer, TaskSerializer, TaskSuggestionSerializer
from .services import (
    accepted_task_defaults, ensure_latest_suggestions, record_task_activity,
    task_activity_snapshot,
)


class TaskPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 200


def _boolean(value):
    return str(value).lower() in {"1", "true", "yes"}


class TaskViewSet(viewsets.ModelViewSet):
    serializer_class = TaskSerializer
    permission_classes = [IsActiveProfile]
    pagination_class = TaskPagination
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["title", "description", "deal__title", "assignee__name", "assignee__email"]
    ordering_fields = ["position", "created_at", "updated_at", "due_date", "priority", "status", "deal__title"]
    ordering = ["position", "due_date", "-created_at"]

    def get_queryset(self):
        queryset = Task.objects.select_related("deal", "assignee", "created_by").prefetch_related("source_suggestions")
        params = self.request.query_params
        if params.get("deal"):
            queryset = queryset.filter(deal_id=params["deal"])
        if params.get("assignee"):
            queryset = queryset.filter(assignee_id=params["assignee"])
        if _boolean(params.get("mine", "false")):
            queryset = queryset.filter(assignee=self.request.user.profile)
        if _boolean(params.get("allocated", "false")):
            queryset = queryset.filter(created_by=self.request.user.profile).exclude(assignee=self.request.user.profile)
        if _boolean(params.get("unassigned", "false")):
            queryset = queryset.filter(assignee__isnull=True)
        if params.get("status"):
            queryset = queryset.filter(status__in=[item for item in params["status"].split(",") if item])
        if params.get("priority"):
            queryset = queryset.filter(priority__in=[item for item in params["priority"].split(",") if item])
        if params.get("due") == "overdue":
            queryset = queryset.exclude(status=TaskStatus.DONE).filter(due_date__lt=timezone.localdate())
        elif params.get("due") == "soon":
            today = timezone.localdate()
            queryset = queryset.exclude(status=TaskStatus.DONE).filter(due_date__range=(today, today + timedelta(days=7)))
        return queryset

    @transaction.atomic
    def perform_create(self, serializer):
        next_position = (Task.objects.aggregate(max_position=Max("position"))["max_position"] or 0) + 1
        task = serializer.save(
            created_by=self.request.user.profile,
            origin=Task.Origin.MANUAL,
            fingerprint="",
            position=next_position,
        )
        record_task_activity(
            task,
            actor=self.request.user.profile,
            action=TaskActivity.Action.CREATED,
            source_context={"origin": Task.Origin.MANUAL},
        )

    @transaction.atomic
    def perform_update(self, serializer):
        locked = Task.objects.select_for_update().get(pk=serializer.instance.pk)
        before = task_activity_snapshot(locked)
        serializer.instance = locked
        task = serializer.save()
        if before.get("status") != task.status and task.status == TaskStatus.IN_REVIEW:
            action = TaskActivity.Action.REVIEW_REQUESTED
        elif before.get("status") == TaskStatus.IN_REVIEW and task.status == TaskStatus.DONE:
            action = TaskActivity.Action.REVIEW_APPROVED
        elif "position" in serializer.validated_data:
            action = TaskActivity.Action.REORDERED
        else:
            action = None
        record_task_activity(
            task,
            actor=self.request.user.profile,
            before=before,
            action=action,
        )

    @action(detail=True, methods=["post"])
    @transaction.atomic
    def move(self, request, pk=None):
        task = Task.objects.select_for_update().get(pk=pk)
        direction = request.data.get("direction")
        if direction not in {"up", "down"}:
            return Response({"detail": "Direction must be up or down."}, status=400)
        candidates = Task.objects.select_for_update().filter(deal=task.deal)
        if direction == "up":
            neighbour = candidates.filter(position__lt=task.position).order_by("-position").first()
        else:
            neighbour = candidates.filter(position__gt=task.position).order_by("position").first()
        if neighbour:
            before = task_activity_snapshot(task)
            task.position, neighbour.position = neighbour.position, task.position
            task.save(update_fields=["position", "updated_at"])
            neighbour.save(update_fields=["position", "updated_at"])
            record_task_activity(task, actor=request.user.profile, before=before, action=TaskActivity.Action.REORDERED)
        return Response(TaskSerializer(task, context={"request": request}).data)

    @transaction.atomic
    def perform_destroy(self, instance):
        instance = Task.objects.select_for_update().select_related("deal").get(pk=instance.pk)
        before = task_activity_snapshot(instance)
        instance.source_suggestions.update(state=TaskSuggestionState.DISMISSED, task=None)
        record_task_activity(
            instance,
            actor=self.request.user.profile,
            action=TaskActivity.Action.DELETED,
            before=before,
            after={},
            source_context={"retention": "task_deleted"},
        )
        instance.delete()

    @action(detail=False, methods=["get"])
    def summary(self, request):
        queryset = Task.objects.all()
        suggestions = TaskSuggestion.objects.filter(state=TaskSuggestionState.PENDING)
        if request.query_params.get("deal"):
            deal_id = request.query_params["deal"]
            queryset = queryset.filter(deal_id=deal_id)
            suggestions = suggestions.filter(deal_id=deal_id)
            deal = Deal.objects.filter(id=deal_id).first()
            if deal:
                ensure_latest_suggestions(deal)
        open_tasks = queryset.exclude(status=TaskStatus.DONE)
        today = timezone.localdate()
        return Response({
            "my_open": open_tasks.filter(assignee=request.user.profile).count(),
            "open": open_tasks.count(),
            "overdue": open_tasks.filter(due_date__lt=today).count(),
            "unassigned": open_tasks.filter(assignee__isnull=True).count(),
            "pending_suggestions": suggestions.count(),
            "allocated": open_tasks.filter(created_by=request.user.profile).exclude(assignee=request.user.profile).count(),
            "in_review": queryset.filter(status=TaskStatus.IN_REVIEW, created_by=request.user.profile).count(),
        })


class TaskCommentViewSet(viewsets.ModelViewSet):
    serializer_class = TaskCommentSerializer
    permission_classes = [IsActiveProfile]
    pagination_class = TaskPagination
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_queryset(self):
        queryset = TaskComment.objects.select_related("task", "author")
        if self.request.query_params.get("task"):
            queryset = queryset.filter(task_id=self.request.query_params["task"])
        return queryset

    @transaction.atomic
    def perform_create(self, serializer):
        comment = serializer.save(author=self.request.user.profile)
        record_task_activity(
            comment.task,
            actor=self.request.user.profile,
            action=TaskActivity.Action.COMMENTED,
            source_context={"comment_id": str(comment.id)},
        )


class TaskSuggestionViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = TaskSuggestionSerializer
    permission_classes = [IsActiveProfile]
    pagination_class = TaskPagination
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["title", "category", "source_section", "deal__title"]
    ordering_fields = ["created_at", "deal__title", "source_section"]
    ordering = ["deal__title", "source_section", "created_at"]

    def get_queryset(self):
        queryset = TaskSuggestion.objects.select_related("deal", "analysis", "task")
        params = self.request.query_params
        if params.get("deal"):
            deal = Deal.objects.filter(id=params["deal"]).first()
            if deal:
                ensure_latest_suggestions(deal)
            queryset = queryset.filter(deal_id=params["deal"])
        state = params.get("state", TaskSuggestionState.PENDING)
        if state:
            queryset = queryset.filter(state=state)
        if _boolean(params.get("latest_only", "true")):
            queryset = queryset.exclude(state=TaskSuggestionState.SUPERSEDED)
        return queryset

    @action(detail=True, methods=["post"])
    def accept(self, request, pk=None):
        with transaction.atomic():
            # Lock only the suggestion row. Joining the nullable task relation here
            # produces an outer join that PostgreSQL cannot lock with FOR UPDATE.
            suggestion = TaskSuggestion.objects.select_for_update().get(pk=pk)
            if suggestion.task_id:
                return Response(TaskSerializer(suggestion.task, context={"request": request}).data)
            existing = Task.objects.filter(deal=suggestion.deal, fingerprint=suggestion.fingerprint).first()
            task = existing or Task.objects.create(
                **accepted_task_defaults(suggestion), created_by=request.user.profile
            )
            suggestion.task = task
            suggestion.state = TaskSuggestionState.ACCEPTED
            suggestion.save(update_fields=["task", "state", "updated_at"])
            record_task_activity(
                task,
                actor=request.user.profile,
                action=TaskActivity.Action.SUGGESTION_ACCEPTED,
                source_context={
                    "origin": Task.Origin.ANALYSIS,
                    "suggestion_id": str(suggestion.id),
                    "analysis_id": str(suggestion.analysis_id) if suggestion.analysis_id else None,
                    "analysis_version": suggestion.analysis_version,
                    "source_section": suggestion.source_section[:500],
                },
            )
        return Response(TaskSerializer(task, context={"request": request}).data, status=status.HTTP_201_CREATED if not existing else status.HTTP_200_OK)

    @action(detail=True, methods=["post"])
    def dismiss(self, request, pk=None):
        suggestion = self.get_object()
        if suggestion.state == TaskSuggestionState.ACCEPTED:
            return Response({"detail": "Accepted suggestions cannot be dismissed."}, status=409)
        suggestion.state = TaskSuggestionState.DISMISSED
        suggestion.save(update_fields=["state", "updated_at"])
        return Response(self.get_serializer(suggestion).data)

    @action(detail=True, methods=["post"])
    def restore(self, request, pk=None):
        suggestion = TaskSuggestion.objects.select_related("deal").get(pk=pk)
        if suggestion.task_id:
            suggestion.state = TaskSuggestionState.ACCEPTED
        else:
            suggestion.state = TaskSuggestionState.PENDING
        suggestion.save(update_fields=["state", "updated_at"])
        return Response(self.get_serializer(suggestion).data)


class TaskActivityViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = TaskActivitySerializer
    permission_classes = [IsActiveProfile]
    pagination_class = TaskPagination
    filter_backends = [filters.OrderingFilter]
    ordering_fields = ["created_at", "action", "task_title"]
    ordering = ["-created_at"]

    def get_queryset(self):
        queryset = TaskActivity.objects.select_related("deal", "actor", "task")
        params = self.request.query_params
        if params.get("deal"):
            queryset = queryset.filter(deal_id=params["deal"])
        if params.get("task"):
            queryset = queryset.filter(task_id_snapshot=params["task"])
        if params.get("action"):
            queryset = queryset.filter(
                action__in=[item for item in params["action"].split(",") if item]
            )
        return queryset
