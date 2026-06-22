"""
URL routing for meetings app.
"""
from django.urls import path, include
from rest_framework.routers import SimpleRouter
from .views import MeetingViewSet, MeetingContactViewSet, MeetingNoteViewSet, MeetingProfileViewSet

router = SimpleRouter()
router.register(r'meeting-notes', MeetingNoteViewSet, basename='meeting-note')
router.register(r'', MeetingViewSet, basename='meeting')
router.register(r'contacts', MeetingContactViewSet, basename='meeting-contact')
router.register(r'profiles', MeetingProfileViewSet, basename='meeting-profile')

urlpatterns = [
    path('', include(router.urls)),
]
