from celery import shared_task

from .models import NewsSource
from .services import ingest_source


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
