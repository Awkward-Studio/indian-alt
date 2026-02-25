from django.urls import path
from .views import AISettingsView, DealChatView

urlpatterns = [
    path('settings/', AISettingsView.as_view(), name='ai-settings'),
    path('deal-chat/', DealChatView.as_view(), name='ai-deal-chat'),
]
