"""
URL routing for banks app.
"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import BankViewSet

router = DefaultRouter()
router.register(r'', BankViewSet, basename='bank')

urlpatterns = [
    path('', include(router.urls)),
]
