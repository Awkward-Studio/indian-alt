from rest_framework.routers import DefaultRouter

from .views import IAThemeViewSet, KnowledgeDocumentViewSet, NewsArticleViewSet, NewsSourceViewSet

router = DefaultRouter()
router.register("documents", KnowledgeDocumentViewSet, basename="knowledge-document")
router.register("themes", IAThemeViewSet, basename="ia-theme")
router.register("sources", NewsSourceViewSet, basename="news-source")
router.register("news", NewsArticleViewSet, basename="industry-news")

urlpatterns = router.urls
