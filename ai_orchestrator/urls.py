from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    AISettingsView, DealChatView, UniversalChatView, 
    AISkillsView, AIConversationViewSet, VMControlView,
    AIAuditLogViewSet, DealHelperView
)

router = DefaultRouter()
router.register(r'conversations', AIConversationViewSet, basename='ai-conversation')
router.register(r'history', AIAuditLogViewSet, basename='ai-history')

urlpatterns = [
    path('', include(router.urls)),
    path('settings/', AISettingsView.as_view(), name='ai-settings'),
    path('skills/', AISkillsView.as_view(), name='ai-skills'),
    path('vm/control/', VMControlView.as_view(), name='ai-vm-control'),
    path('deal-chat/', DealChatView.as_view(), name='ai-deal-chat'),
    path('deal-helper/<str:action>/', DealHelperView.as_view(), name='ai-deal-helper'),
    path('universal-chat/', UniversalChatView.as_view(), name='ai-universal-chat'),
]
