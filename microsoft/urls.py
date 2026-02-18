"""
URL routing for microsoft app.

All URLs are mounted at /api/microsoft/ in config/urls.py, giving:
    /api/microsoft/emails/accounts/     — email account CRUD
    /api/microsoft/emails/emails/       — email list/detail (read-only)
    /api/microsoft/emails/fetch/        — email fetch triggers
    /api/microsoft/onedrive/files/      — OneDrive file/folder browsing
"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import EmailAccountViewSet, EmailViewSet, EmailFetchViewSet, OneDriveListView

# Email-related routers
email_router = DefaultRouter()
email_router.register(r'accounts', EmailAccountViewSet, basename='emailaccount')
email_router.register(r'emails', EmailViewSet, basename='email')
email_router.register(r'fetch', EmailFetchViewSet, basename='emailfetch')

urlpatterns = [
    # Email endpoints: /api/microsoft/emails/...
    path('emails/', include(email_router.urls)),

    # OneDrive endpoints: /api/microsoft/onedrive/...
    path('onedrive/files/', OneDriveListView.as_view(), name='onedrive-list'),
]
