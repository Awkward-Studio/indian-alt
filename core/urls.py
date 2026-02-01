"""
URL routing for core app (Version only).
"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import VersionViewSet

router = DefaultRouter()
router.register(r'versions', VersionViewSet, basename='version')

urlpatterns = [
    path('', include(router.urls)),
]
