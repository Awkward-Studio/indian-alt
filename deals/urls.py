"""
URL routing for deals app.
"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import DealViewSet, DealDocumentViewSet

router = DefaultRouter()
router.register(r'documents', DealDocumentViewSet, basename='deal-document')
router.register(r'', DealViewSet, basename='deal')

urlpatterns = [
    path('', include(router.urls)),
]
