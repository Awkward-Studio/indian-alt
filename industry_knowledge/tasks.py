from celery import shared_task

from django.utils import timezone

from .models import Industry, NewsSource
from .services import ingest_source, pull_industry_news


@shared_task
def ingest_industry_news():
    results = {}
    for source in NewsSource.objects.filter(is_active=True):
        try:
            results[source.name] = ingest_source(source)
        except Exception as exc:
            source.last_error = str(exc)[:2000]
            source.save(update_fields=["last_error", "updated_at"])
            results[source.name] = {"error": str(exc)}
    return results


@shared_task
def refresh_industry_research(industry_id):
    industry = Industry.objects.get(pk=industry_id)
    industry.research_status = Industry.ResearchStatus.RUNNING
    industry.research_error = ""
    industry.save(update_fields=["research_status", "research_error", "updated_at"])
    try:
        articles = pull_industry_news(industry)
        industry.research_status = Industry.ResearchStatus.COMPLETE
        industry.last_researched_at = timezone.now()
        industry.save(update_fields=["research_status", "last_researched_at", "updated_at"])
        return {"industry_id": str(industry.id), "articles": len(articles)}
    except Exception as exc:
        industry.research_status = Industry.ResearchStatus.FAILED
        industry.research_error = str(exc)[:2000]
        industry.save(update_fields=["research_status", "research_error", "updated_at"])
        raise
