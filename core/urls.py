"""
URL routing for core app (Version only).
"""
from django.urls import path, include, re_path
from rest_framework.routers import DefaultRouter
from .views import VersionViewSet, HealthCheckView

router = DefaultRouter()
router.register(r'versions', VersionViewSet, basename='version')

urlpatterns = [
    re_path(r'^health/?$', HealthCheckView.as_view(), name='health'),
    path('', include(router.urls)),
]
