from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    AISettingsView, AIConnectionStatusView, ForexRateView, DealChatView, UniversalChatView,
    AISkillsView, DealIndustrySkillsView, DealIndustrySkillAssignmentView,
    DealIndustrySkillRunView, AIConversationViewSet, VMControlView,
    AIAuditLogViewSet, DealHelperView
)

router = DefaultRouter()
router.register(r'conversations', AIConversationViewSet, basename='ai-conversation')
router.register(r'history', AIAuditLogViewSet, basename='ai-history')

urlpatterns = [
    path('', include(router.urls)),
    path('settings/', AISettingsView.as_view(), name='ai-settings'),
    path('settings/connection-status/', AIConnectionStatusView.as_view(), name='ai-settings-connection-status'),
    path('forex-rate/', ForexRateView.as_view(), name='ai-forex-rate'),
    path('skills/', AISkillsView.as_view(), name='ai-skills'),
    path('skills/deals/<uuid:deal_id>/', DealIndustrySkillsView.as_view(), name='deal-industry-skills'),
    path('skills/deals/<uuid:deal_id>/<uuid:skill_id>/', DealIndustrySkillAssignmentView.as_view(), name='deal-industry-skill-assignment'),
    path('skills/deals/<uuid:deal_id>/<uuid:skill_id>/run/', DealIndustrySkillRunView.as_view(), name='deal-industry-skill-run'),
    path('vm/control/', VMControlView.as_view(), name='ai-vm-control'),
    path('deal-chat/', DealChatView.as_view(), name='ai-deal-chat'),
    path('deal-helper/<str:action>/', DealHelperView.as_view(), name='ai-deal-helper'),
    path('universal-chat/', UniversalChatView.as_view(), name='ai-universal-chat'),
]
