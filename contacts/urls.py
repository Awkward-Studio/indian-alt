"""
URL routing for contacts app.
"""
from django.urls import path, include
from rest_framework.routers import SimpleRouter
from .views import BankerAnalyticsViewSet, ContactCardExtractionViewSet, ContactViewSet

router = SimpleRouter()
router.register(r'analytics', BankerAnalyticsViewSet, basename='banker-analytics')
router.register(r'card-extractions', ContactCardExtractionViewSet, basename='contact-card-extraction')
router.register(r'', ContactViewSet, basename='contact')

urlpatterns = [
    path('', include(router.urls)),
]
