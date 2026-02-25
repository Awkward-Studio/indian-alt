from django.urls import path
from .views import AISettingsView, DealChatView, UniversalChatView

urlpatterns = [
    path('settings/', AISettingsView.as_view(), name='ai-settings'),
    path('deal-chat/', DealChatView.as_view(), name='ai-deal-chat'),
    path('universal-chat/', UniversalChatView.as_view(), name='ai-universal-chat'),
]
