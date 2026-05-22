"""
URL routing for deals app.
"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import DealViewSet, DealDocumentViewSet, VentureIntelligencePreviewView, DealEnrichView

router = DefaultRouter()
router.register(r'documents', DealDocumentViewSet, basename='deal-document')
router.register(r'', DealViewSet, basename='deal')

urlpatterns = [
    path('venture-intelligence/preview/', VentureIntelligencePreviewView.as_view(), name='vi-preview'),
    path('<uuid:pk>/enrich/', DealEnrichView.as_view(), name='deal-enrich'),
    path('', include(router.urls)),
]
