"""
URL routing for core app (Version only).
"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import VersionViewSet, HealthCheckView

router = DefaultRouter()
router.register(r'versions', VersionViewSet, basename='version')

urlpatterns = [
    path('health/', HealthCheckView.as_view(), name='health'),
    path('', include(router.urls)),
]
