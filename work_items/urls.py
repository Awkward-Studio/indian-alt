from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import TaskActivityViewSet, TaskCommentViewSet, TaskSuggestionViewSet, TaskViewSet


router = DefaultRouter()
router.register("suggestions", TaskSuggestionViewSet, basename="task-suggestion")
router.register("activities", TaskActivityViewSet, basename="task-activity")
router.register("comments", TaskCommentViewSet, basename="task-comment")
router.register("", TaskViewSet, basename="task")

urlpatterns = [path("", include(router.urls))]
