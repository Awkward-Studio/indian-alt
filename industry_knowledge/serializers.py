from rest_framework import serializers

from deals.models import Deal, DealDocument
from meetings.models import MeetingNote
from .models import IATheme, Industry, IndustryDocument, IndustryNewsArticle, KnowledgeDocument, NewsArticle, NewsSource
from .permissions import is_admin


class KnowledgeDocumentSerializer(serializers.ModelSerializer):
    source_url = serializers.SerializerMethodField()
    source_deal = serializers.SerializerMethodField()
    is_indexed = serializers.SerializerMethodField()
    published_by_name = serializers.CharField(source="published_by.name", read_only=True)
    deal_document_id = serializers.PrimaryKeyRelatedField(source="deal_document", queryset=DealDocument.objects.all(), required=False, allow_null=True)
    meeting_note_id = serializers.PrimaryKeyRelatedField(source="meeting_note", queryset=MeetingNote.objects.all(), required=False, allow_null=True)

    class Meta:
        model = KnowledgeDocument
        fields = "__all__"
        read_only_fields = ("id", "deal_document", "meeting_note", "published_by", "created_at", "updated_at")

    def validate(self, attrs):
        document = attrs.get("deal_document")
        note = attrs.get("meeting_note")
        if bool(document) == bool(note):
            raise serializers.ValidationError("Choose exactly one report or meeting transcript.")
        expected = KnowledgeDocument.Kind.REPORT if document else KnowledgeDocument.Kind.TRANSCRIPT
        if attrs.get("kind") != expected:
            raise serializers.ValidationError({"kind": f"Use {expected} for the selected source."})
        request = self.context.get("request")
        profile = getattr(getattr(request, "user", None), "profile", None)
        if note and not is_admin(request.user) and note.created_by_id != getattr(profile, "id", None):
            raise serializers.ValidationError("Only the note owner or an administrator can publish this transcript.")
        if note and not attrs.get("confidentiality", "").strip():
            raise serializers.ValidationError({"confidentiality": "Record the transcript confidentiality level."})
        source = document or note
        if source and not source.is_indexed:
            raise serializers.ValidationError("Index the source before publishing it to Industry Knowledge.")
        return attrs

    def get_source_url(self, obj):
        return obj.deal_document.file_url if obj.deal_document_id else None

    def get_source_deal(self, obj):
        if obj.deal_document_id:
            return {"id": str(obj.deal_document.deal_id), "title": obj.deal_document.deal.title}
        deal = obj.meeting_note.deals.first() if obj.meeting_note_id else None
        return {"id": str(deal.id), "title": deal.title} if deal else None

    def get_is_indexed(self, obj):
        return obj.deal_document.is_indexed if obj.deal_document_id else obj.meeting_note.is_indexed


class IAThemeSerializer(serializers.ModelSerializer):
    is_subscribed = serializers.SerializerMethodField()

    class Meta:
        model = IATheme
        fields = "__all__"
        read_only_fields = ("id", "subscribed_by", "created_at", "updated_at")

    def get_is_subscribed(self, obj):
        request = self.context.get("request")
        return bool(request and request.user.is_authenticated and obj.subscribed_by.filter(id=request.user.id).exists())


class NewsSourceSerializer(serializers.ModelSerializer):
    class Meta:
        model = NewsSource
        fields = "__all__"
        read_only_fields = ("id", "last_fetched_at", "last_error", "created_at", "updated_at")


class NewsArticleSerializer(serializers.ModelSerializer):
    source_name = serializers.CharField(source="source.name", read_only=True)
    themes = IAThemeSerializer(many=True, read_only=True)
    theme_ids = serializers.PrimaryKeyRelatedField(source="themes", queryset=IATheme.objects.all(), many=True, write_only=True, required=False)
    linked_deal_ids = serializers.PrimaryKeyRelatedField(source="linked_deals", queryset=Deal.objects.all(), many=True, write_only=True, required=False)
    is_saved = serializers.SerializerMethodField()
    is_dismissed = serializers.SerializerMethodField()

    class Meta:
        model = NewsArticle
        fields = "__all__"
        read_only_fields = ("id", "source", "created_at", "updated_at", "saved_by", "dismissed_by")

    def get_is_saved(self, obj):
        request = self.context.get("request")
        return bool(request and request.user.is_authenticated and obj.saved_by.filter(id=request.user.id).exists())

    def get_is_dismissed(self, obj):
        request = self.context.get("request")
        return bool(request and request.user.is_authenticated and obj.dismissed_by.filter(id=request.user.id).exists())


class IndustryDocumentSerializer(serializers.ModelSerializer):
    file_url = serializers.SerializerMethodField()
    deal_title = serializers.CharField(source="deal_document.deal.title", read_only=True, allow_null=True)
    uploaded_by_name = serializers.CharField(source="uploaded_by.name", read_only=True, allow_null=True)

    class Meta:
        model = IndustryDocument
        fields = [
            "id", "industry_id", "title", "file_name", "document_type",
            "file_size", "file_url", "extracted_text", "deal_document_id",
            "deal_title", "uploaded_by_name", "created_at", "updated_at",
        ]

    def get_file_url(self, obj):
        if obj.file:
            request = self.context.get("request")
            return request.build_absolute_uri(obj.file.url) if request else obj.file.url
        if obj.deal_document:
            return obj.deal_document.file_url
        return None


class IndustryNewsArticleSerializer(serializers.ModelSerializer):
    class Meta:
        model = IndustryNewsArticle
        fields = ["id", "industry_id", "title", "url", "source_name", "summary", "category", "published_at", "created_at"]


class DealSummaryForIndustrySerializer(serializers.ModelSerializer):
    class Meta:
        model = Deal
        fields = [
            "id", "title", "deal_status", "fund",
            "funding_ask", "funding_ask_for", "received_at", "deal_summary",
            "city", "is_female_led", "created_at",
        ]


class IndustryListSerializer(serializers.ModelSerializer):
    deals_count = serializers.IntegerField(read_only=True)
    documents_count = serializers.IntegerField(read_only=True)
    news_count = serializers.IntegerField(read_only=True)
    parent_name = serializers.CharField(source="parent.name", read_only=True, allow_null=True)

    class Meta:
        model = Industry
        fields = [
            "id", "name", "parent", "parent_name", "overview", "context", "market_size", "growth_rate",
            "classification_status", "classification_basis", "research_status", "last_researched_at", "research_error",
            "summary_status", "summary_error", "summary_sources", "last_summarized_at",
            "deals_count", "documents_count", "news_count", "created_at", "updated_at",
        ]


class IndustryDetailSerializer(serializers.ModelSerializer):
    deals_count = serializers.SerializerMethodField()
    documents = serializers.SerializerMethodField()
    news_articles = serializers.SerializerMethodField()
    deals = serializers.SerializerMethodField()
    parent_name = serializers.CharField(source="parent.name", read_only=True, allow_null=True)
    sub_industries = serializers.SerializerMethodField()

    class Meta:
        model = Industry
        fields = [
            "id", "name", "parent", "parent_name", "overview", "context", "market_size", "growth_rate",
            "classification_status", "classification_basis", "research_status", "last_researched_at", "research_error",
            "summary_status", "summary_error", "summary_sources", "last_summarized_at",
            "documents", "news_articles", "deals", "sub_industries", "deals_count", "created_at", "updated_at",
        ]

    def get_deals_count(self, obj):
        names = [obj.name, *obj.sub_industries.values_list("name", flat=True)]
        return Deal.objects.filter(industry__in=names).count()

    def get_sub_industries(self, obj):
        children = list(obj.sub_industries.select_related("parent").prefetch_related("documents", "news_articles"))
        for child in children:
            child.deals_count = Deal.objects.filter(industry=child.name).count()
            child.documents_count = child.documents.count()
            child.news_count = child.news_articles.count()
        return IndustryListSerializer(children, many=True, context=self.context).data

    def get_documents(self, obj):
        ids = [obj.id, *obj.sub_industries.values_list("id", flat=True)]
        rows = IndustryDocument.objects.filter(industry_id__in=ids).select_related("deal_document__deal", "uploaded_by")
        return IndustryDocumentSerializer(rows, many=True, context=self.context).data

    def get_news_articles(self, obj):
        ids = [obj.id, *obj.sub_industries.values_list("id", flat=True)]
        rows = IndustryNewsArticle.objects.filter(industry_id__in=ids).order_by("-published_at", "-created_at")[:100]
        return IndustryNewsArticleSerializer(rows, many=True, context=self.context).data

    def get_deals(self, obj):
        names = [obj.name, *obj.sub_industries.values_list("name", flat=True)]
        deals = Deal.objects.filter(industry__in=names).order_by("-received_at", "-created_at")
        return DealSummaryForIndustrySerializer(deals, many=True).data
