"""
URL routing for requests app.
"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import RequestViewSet

router = DefaultRouter()
router.register(r'', RequestViewSet, basename='request')

urlpatterns = [
    path('', include(router.urls)),
]
