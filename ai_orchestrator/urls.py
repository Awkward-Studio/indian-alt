from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import AISettingsView, DealChatView, UniversalChatView, AISkillsView, AIConversationViewSet, VMControlView

router = DefaultRouter()
router.register(r'conversations', AIConversationViewSet, basename='ai-conversation')

urlpatterns = [
    path('', include(router.urls)),
    path('settings/', AISettingsView.as_view(), name='ai-settings'),
    path('skills/', AISkillsView.as_view(), name='ai-skills'),
    path('vm/control/', VMControlView.as_view(), name='ai-vm-control'),
    path('deal-chat/', DealChatView.as_view(), name='ai-deal-chat'),
    path('universal-chat/', UniversalChatView.as_view(), name='ai-universal-chat'),
]

