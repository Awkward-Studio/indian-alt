from django.db.models import Q
from rest_framework import filters, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import IATheme, KnowledgeDocument, NewsArticle, NewsSource
from .permissions import CanPublishKnowledge, IsAdminOrReadOnly, is_admin
from .serializers import IAThemeSerializer, KnowledgeDocumentSerializer, NewsArticleSerializer, NewsSourceSerializer
from .services import ingest_source


class KnowledgeDocumentViewSet(viewsets.ModelViewSet):
    serializer_class = KnowledgeDocumentSerializer
    permission_classes = [CanPublishKnowledge]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["title", "publisher", "sector", "themes", "confidentiality"]
    ordering_fields = ["published_at", "created_at", "title"]
    ordering = ["-published_at", "-created_at"]

    def get_queryset(self):
        queryset = KnowledgeDocument.objects.select_related("deal_document__deal", "meeting_note", "published_by").prefetch_related("meeting_note__deals")
        if not is_admin(self.request.user):
            profile = getattr(self.request.user, "profile", None)
            queryset = queryset.filter(Q(visibility=KnowledgeDocument.Visibility.INTERNAL) | Q(published_by=profile))
        for field in ("kind", "sector", "visibility"):
            value = self.request.query_params.get(field)
            if value:
                queryset = queryset.filter(**{field: value})
        theme = self.request.query_params.get("theme")
        return queryset.filter(themes__contains=[theme]) if theme else queryset

    def perform_create(self, serializer):
        serializer.save(published_by=self.request.user.profile)


class IAThemeViewSet(viewsets.ModelViewSet):
    queryset = IATheme.objects.all()
    serializer_class = IAThemeSerializer
    permission_classes = [IsAdminOrReadOnly]

    @action(detail=True, methods=["post"], permission_classes=[IsAuthenticated])
    def subscribe(self, request, pk=None):
        theme = self.get_object()
        theme.subscribed_by.add(request.user)
        return Response(self.get_serializer(theme).data)

    @action(detail=True, methods=["post"], permission_classes=[IsAuthenticated])
    def unsubscribe(self, request, pk=None):
        theme = self.get_object()
        theme.subscribed_by.remove(request.user)
        return Response(self.get_serializer(theme).data)


class NewsSourceViewSet(viewsets.ModelViewSet):
    queryset = NewsSource.objects.all()
    serializer_class = NewsSourceSerializer
    permission_classes = [IsAdminOrReadOnly]

    @action(detail=True, methods=["post"])
    def ingest(self, request, pk=None):
        source = self.get_object()
        try:
            return Response(ingest_source(source))
        except Exception as exc:
            source.last_error = str(exc)[:2000]
            source.save(update_fields=["last_error", "updated_at"])
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


class NewsArticleViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = NewsArticleSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["title", "summary", "companies", "source__name", "themes__name"]
    ordering = ["-published_at", "-created_at"]

    def get_queryset(self):
        queryset = NewsArticle.objects.select_related("source").prefetch_related("themes", "saved_by", "dismissed_by", "linked_deals")
        source = self.request.query_params.get("source")
        theme = self.request.query_params.get("theme")
        saved = self.request.query_params.get("saved")
        include_dismissed = self.request.query_params.get("include_dismissed") == "true"
        if source:
            queryset = queryset.filter(source_id=source)
        if theme:
            queryset = queryset.filter(themes__id=theme)
        if saved == "true":
            queryset = queryset.filter(saved_by=self.request.user)
        if not include_dismissed:
            queryset = queryset.exclude(dismissed_by=self.request.user)
        return queryset.distinct()

    @action(detail=True, methods=["post"])
    def save(self, request, pk=None):
        article = self.get_object()
        article.dismissed_by.remove(request.user)
        article.saved_by.add(request.user)
        return Response(self.get_serializer(article).data)

    @action(detail=True, methods=["post"])
    def dismiss(self, request, pk=None):
        article = self.get_object()
        article.saved_by.remove(request.user)
        article.dismissed_by.add(request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"], url_path="link-deal")
    def link_deal(self, request, pk=None):
        deal_id = request.data.get("deal_id")
        if not deal_id:
            return Response({"error": "deal_id is required."}, status=400)
        article = self.get_object()
        article.linked_deals.add(deal_id)
        return Response(self.get_serializer(article).data)
