from django.db.models import Q
from rest_framework import filters, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from deals.models import DealDocument
from .models import (
    IATheme,
    Industry,
    IndustryDocument,
    IndustryNewsArticle,
    KnowledgeDocument,
    NewsArticle,
    NewsSource,
)
from .permissions import CanPublishKnowledge, IsAdminOrReadOnly, is_admin
from .serializers import (
    IAThemeSerializer,
    IndustryDetailSerializer,
    IndustryDocumentSerializer,
    IndustryListSerializer,
    IndustryNewsArticleSerializer,
    KnowledgeDocumentSerializer,
    NewsArticleSerializer,
    NewsSourceSerializer,
)
from .services import (
    get_industry_deal_counts,
    ingest_source,
    merge_industries,
    pull_industry_news,
    sync_industries_from_deals,
)


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


class IndustryViewSet(viewsets.ModelViewSet):
    serializer_class = IndustryListSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = []

    def get_serializer_class(self):
        if self.action in ["retrieve", "partial_update", "update"]:
            return IndustryDetailSerializer
        return IndustryListSerializer

    def get_queryset(self):
        if self.action != "list":
            return Industry.objects.all()

        sync_industries_from_deals()
        counts_map = get_industry_deal_counts()
        queryset = Industry.objects.all().prefetch_related("documents", "news_articles")

        search = self.request.query_params.get("search")
        if search:
            queryset = queryset.filter(
                Q(name__icontains=search) | Q(overview__icontains=search) | Q(context__icontains=search)
            )

        ordering = self.request.query_params.get("ordering", "name")
        industries = list(queryset)

        for ind in industries:
            ind.deals_count = counts_map.get(ind.name.strip(), 0)
            ind.documents_count = ind.documents.count()
            ind.news_count = ind.news_articles.count()

        if ordering == "-deals_count":
            industries.sort(key=lambda x: (x.deals_count, x.name.lower()), reverse=True)
        elif ordering == "deals_count":
            industries.sort(key=lambda x: (x.deals_count, x.name.lower()))
        elif ordering == "-name":
            industries.sort(key=lambda x: x.name.lower(), reverse=True)
        else:
            industries.sort(key=lambda x: (-x.deals_count, x.name.lower()))

        return industries

    def list(self, request, *args, **kwargs):
        industries = self.get_queryset()
        serializer = self.get_serializer(industries, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=["post"], url_path="pull-news")
    def pull_news(self, request, pk=None):
        industry = self.get_object()
        articles = pull_industry_news(industry)
        return Response(IndustryNewsArticleSerializer(articles, many=True).data)

    @action(detail=True, methods=["post"], url_path="upload-document")
    def upload_document(self, request, pk=None):
        industry = self.get_object()
        file_obj = request.FILES.get("file")
        if not file_obj:
            return Response({"error": "No file provided."}, status=400)
        if file_obj.size > 25 * 1024 * 1024:
            return Response({"error": "File size exceeds 25 MB limit."}, status=400)

        file_content = file_obj.read()
        file_name = file_obj.name

        from ai_orchestrator.services.document_processor import DocumentProcessorService
        try:
            extraction = DocumentProcessorService().get_native_extraction_result(file_content, file_name)
            extracted_text = (extraction.get("raw_extracted_text") or extraction.get("text") or "").strip()
        except Exception:
            extracted_text = ""

        from django.core.files.base import ContentFile
        doc = IndustryDocument.objects.create(
            industry=industry,
            title=request.data.get("title") or file_name,
            file_name=file_name,
            document_type=request.data.get("document_type") or "Industry Report",
            extracted_text=extracted_text,
            file_size=len(file_content),
            uploaded_by=request.user if request.user.is_authenticated else None,
        )
        doc.file.save(file_name, ContentFile(file_content), save=True)
        return Response(IndustryDocumentSerializer(doc, context={"request": request}).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="link-deal-document")
    def link_deal_document(self, request, pk=None):
        industry = self.get_object()
        deal_document_id = request.data.get("deal_document_id")
        if not deal_document_id:
            return Response({"error": "deal_document_id is required."}, status=400)
        deal_doc = DealDocument.objects.filter(id=deal_document_id).first()
        if not deal_doc:
            return Response({"error": "Deal document not found."}, status=404)

        doc = IndustryDocument.objects.create(
            industry=industry,
            title=deal_doc.title,
            file_name=deal_doc.title,
            document_type=deal_doc.document_type or "Deal Document",
            extracted_text=deal_doc.normalized_text or deal_doc.extracted_text or "",
            deal_document=deal_doc,
            uploaded_by=request.user if request.user.is_authenticated else None,
        )
        return Response(IndustryDocumentSerializer(doc, context={"request": request}).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post"], url_path="merge")
    def merge(self, request):
        source_id = request.data.get("source_industry_id")
        source_ids = request.data.get("source_industry_ids") or ([] if not source_id else [source_id])
        target_id = request.data.get("target_industry_id")
        target_name = request.data.get("target_name")

        try:
            result = merge_industries(
                source_ids=source_ids,
                target_id=target_id,
                target_name=target_name,
                user=request.user,
            )
            return Response(result)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=400)
        except Exception as exc:
            return Response({"error": f"Merge failed: {exc}"}, status=500)

