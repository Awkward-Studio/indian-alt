from django.contrib import admin

from .models import IATheme, KnowledgeDocument, NewsArticle, NewsSource

admin.site.register(KnowledgeDocument)
admin.site.register(IATheme)
admin.site.register(NewsSource)
admin.site.register(NewsArticle)
