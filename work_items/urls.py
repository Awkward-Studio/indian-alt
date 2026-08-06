from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import TaskSuggestionViewSet, TaskViewSet


router = DefaultRouter()
router.register("suggestions", TaskSuggestionViewSet, basename="task-suggestion")
router.register("", TaskViewSet, basename="task")

urlpatterns = [path("", include(router.urls))]
