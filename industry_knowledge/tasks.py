from celery import shared_task

from django.utils import timezone

from .models import Industry, IndustryDocument, IndustryNewsArticle, NewsSource
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


def _bounded(value, limit):
    return (value or "").strip()[:limit]


@shared_task
def generate_industry_summary(industry_id):
    import json
    import re

    from ai_orchestrator.services.embedding_processor import EmbeddingService
    from ai_orchestrator.services.llm_providers import VLLMProviderService
    from ai_orchestrator.services.runtime import AIRuntimeService
    from deals.models import Deal

    industry = Industry.objects.get(pk=industry_id)
    industry.summary_status = Industry.ResearchStatus.RUNNING
    industry.summary_error = ""
    industry.save(update_fields=["summary_status", "summary_error", "updated_at"])
    try:
        child_names = list(industry.sub_industries.values_list("name", flat=True))
        industry_names = [industry.name, *child_names]
        deals = list(Deal.objects.filter(industry__in=industry_names).order_by("-received_at", "-created_at"))
        deal_ids = [str(deal.id) for deal in deals]

        documents = list(
            IndustryDocument.objects.filter(industry__name__in=industry_names)
            .exclude(extracted_text="")
            .select_related("industry")
            .order_by("-created_at")[:10]
        )
        articles = list(
            IndustryNewsArticle.objects.filter(industry__name__in=industry_names)
            .select_related("industry")
            .order_by("-published_at", "-created_at")[:30]
        )

        semantic_chunks = []
        if deal_ids:
            query = f"{industry.name} India market size TAM growth segments investment trends"
            try:
                semantic_chunks = EmbeddingService().search_global_chunks(query, limit=10, deal_ids=deal_ids)
            except Exception:
                semantic_chunks = []

        internal_reports = "\n\n".join(
            f"[IA REPORT {index}; industry={doc.industry.name}; title={doc.title}]\n{_bounded(doc.extracted_text, 2500)}"
            for index, doc in enumerate(documents, 1)
        )
        web_evidence = "\n".join(
            f"[{article.category}; industry={article.industry.name}; source={article.source_name}; url={article.url}] "
            f"{article.title}. {_bounded(article.summary, 600)}"
            for article in articles
        )
        deal_evidence = "\n".join(
            f"[IA DEAL; industry={deal.industry}; title={deal.title}] {_bounded(deal.deal_summary, 500)}"
            for deal in deals[:40]
        )
        semantic_evidence = "\n\n".join(
            f"[SEMANTIC SUPPORT; deal={chunk.deal.title if chunk.deal else 'Unknown'}; "
            f"industry={chunk.deal.industry if chunk.deal else 'Unknown'}; source={chunk.source_type}:{chunk.source_id}]\n"
            f"{_bounded(chunk.content, 1000)}"
            for chunk in semantic_chunks
        )

        system = (
            "You prepare evidence-led sector summaries for an investment team. Return JSON only. "
            "Use IA reports as the strongest evidence, web reports and transactions next, IA deal summaries next, "
            "and semantic support only as lower-priority corroboration. Never use evidence outside the supplied umbrella industries. "
            "Do not invent TAM or growth figures. If evidence conflicts, state the range or uncertainty."
        )
        prompt = f"""Create the umbrella-industry summary for {industry.name}.
Included industries: {', '.join(industry_names)}

IA REPORTS:
{internal_reports or 'None'}

WEB REPORTS AND TRANSACTIONS:
{web_evidence or 'None'}

IA DEAL SUMMARIES:
{deal_evidence or 'None'}

LOWER-PRIORITY SEMANTIC SUPPORT FROM INCLUDED INDUSTRIES:
{semantic_evidence or 'None'}

Return exactly this JSON shape:
{{"market_size":"short TAM value with year and geography, or Not established", "growth_rate":"short growth value with period, or Not established", "overview":"clear sector summary covering structure, major sub-segments, TAM and growth evidence", "context":"what matters for IA, including deal patterns, opportunities, risks and open questions"}}"""
        result = VLLMProviderService().execute_standard({
            "model": AIRuntimeService.get_text_model(),
            "system": system,
            "prompt": prompt,
            "options": {"temperature": 0.1, "max_tokens": 2600},
        }, timeout=None)
        raw = str(result.get("response") or "")
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        payload = json.loads(match.group(0)) if match else {}
        required = ("market_size", "growth_rate", "overview", "context")
        if not all(isinstance(payload.get(key), str) and payload[key].strip() for key in required):
            raise ValueError("The AI response did not contain all required summary fields.")

        industry.market_size = _bounded(payload["market_size"], 255)
        industry.growth_rate = _bounded(payload["growth_rate"], 255)
        industry.overview = payload["overview"].strip()
        industry.context = payload["context"].strip()
        industry.summary_sources = {
            "umbrella": industry.name,
            "included_industries": industry_names,
            "ia_reports": len(documents),
            "web_reports": sum(article.category == IndustryNewsArticle.Category.REPORT for article in articles),
            "transactions": sum(article.category == IndustryNewsArticle.Category.TRANSACTION for article in articles),
            "ia_deals": len(deals),
            "semantic_matches": len(semantic_chunks),
        }
        industry.summary_status = Industry.ResearchStatus.COMPLETE
        industry.last_summarized_at = timezone.now()
        industry.save(update_fields=[
            "market_size", "growth_rate", "overview", "context", "summary_sources",
            "summary_status", "last_summarized_at", "updated_at",
        ])
        return {"industry_id": str(industry.id), **industry.summary_sources}
    except Exception as exc:
        industry.summary_status = Industry.ResearchStatus.FAILED
        industry.summary_error = str(exc)[:2000]
        industry.save(update_fields=["summary_status", "summary_error", "updated_at"])
        raise
