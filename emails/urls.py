"""
URL routing for emails app.
"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import EmailAccountViewSet, EmailViewSet, EmailFetchViewSet

router = DefaultRouter()
router.register(r'accounts', EmailAccountViewSet, basename='emailaccount')
router.register(r'emails', EmailViewSet, basename='email')
router.register(r'fetch', EmailFetchViewSet, basename='emailfetch')

urlpatterns = [
    path('', include(router.urls)),
]
