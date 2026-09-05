import email.utils
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone as dt_timezone
from urllib.parse import urlparse

import requests
from django.utils import timezone

from .models import IATheme, NewsArticle


def _search_entries(source):
    if source.requires_licensed_api and not source.feed_url:
        raise ValueError("This publisher requires an approved feed or API URL.")
    from ai_orchestrator.services.search_provider import SearXNGProviderService

    domain = urlparse(source.homepage_url).netloc.removeprefix("www.")
    if not domain:
        raise ValueError("Configure the publisher homepage before running web discovery.")
    provider = SearXNGProviderService()
    results = provider.search_results(
        f"site:{domain} latest startup investment industry news India", num_results=30,
        context={"purpose": "industry news", "domain": domain, "geography": "India"},
    )
    return [{
        "title": item.get("title", ""), "url": item.get("url", ""),
        "summary": item.get("snippet", ""), "author": "",
        "published_at": item.get("published_date", ""),
    } for item in results if (
        (urlparse(item.get("url", "")).hostname or "") == domain
        or (urlparse(item.get("url", "")).hostname or "").endswith(f".{domain}")
    )]


def _ai_classifications(entries, themes):
    if not entries or not themes:
        return {}
    system = "Classify public news metadata. Return JSON only. Do not add facts absent from the supplied title and snippet."
    prompt = json.dumps({
        "instruction": "For each URL return themes chosen only from allowed_themes and company names explicitly mentioned.",
        "allowed_themes": [{"name": theme.name, "keywords": theme.keywords} for theme in themes],
        "articles": entries,
        "response_shape": {"classifications": [{"url": "string", "themes": ["string"], "companies": ["string"]}]},
    }, ensure_ascii=False)
    try:
        from ai_orchestrator.services.llm_providers import VLLMProviderService
        from ai_orchestrator.services.runtime import AIRuntimeService
        audit = AIRuntimeService.create_audit_log(
            source_type="industry_news_classification", source_id=None,
            context_label="Scheduled industry news classification", status="RUNNING",
            system_prompt=system, user_prompt=prompt,
            source_metadata={"article_count": len(entries)},
        )
        result = VLLMProviderService().execute_standard({
            "model": AIRuntimeService.get_text_model(), "system": system, "prompt": prompt,
            "options": {"temperature": 0.0, "max_tokens": 3000},
        }, timeout=None)
        raw = str(result.get("response") or "")
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        parsed = json.loads(match.group(0)) if match else {}
        audit.raw_response = raw
        audit.parsed_json = parsed
        audit.status = "COMPLETED"
        audit.is_success = True
        audit.save(update_fields=["raw_response", "parsed_json", "status", "is_success"])
        return {row["url"]: row for row in parsed.get("classifications", []) if isinstance(row, dict) and row.get("url")}
    except Exception as exc:
        if "audit" in locals():
            audit.status = "FAILED"
            audit.is_success = False
            audit.error_message = str(exc)
            audit.save(update_fields=["status", "is_success", "error_message"])
        return {}


def _text(node, names):
    for child in node.iter():
        if child.tag.split("}")[-1] in names and child.text:
            return child.text.strip()
    return ""


def _link(node):
    for child in node.iter():
        if child.tag.split("}")[-1] == "link":
            return (child.attrib.get("href") or child.text or "").strip()
    return ""


def _published(value):
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed


def ingest_source(source):
    themes = list(IATheme.objects.filter(is_active=True))
    if source.feed_url:
        response = requests.get(source.feed_url, timeout=15, headers={"User-Agent": "IndiaAlternativesKnowledge/1.0"})
        response.raise_for_status()
        root = ET.fromstring(response.content)
        nodes = [node for node in root.iter() if node.tag.split("}")[-1] in {"item", "entry"}]
        entries = [{
            "title": _text(entry, {"title"}), "url": _link(entry),
            "summary": _text(entry, {"description", "summary", "content"}),
            "author": _text(entry, {"author", "creator"}),
            "published_at": _text(entry, {"pubDate", "published", "updated"}),
        } for entry in nodes[:100]]
        classifications = {}
    else:
        entries = _search_entries(source)
        classifications = _ai_classifications(entries, themes)
    created = 0
    for entry in entries[:100]:
        title = entry["title"]
        url = entry["url"]
        if not title or not url:
            continue
        summary = entry["summary"]
        classification = classifications.get(url, {})
        article, was_created = NewsArticle.objects.update_or_create(
            url=url,
            defaults={
                "source": source,
                "title": title[:600],
                "summary": summary,
                "author": entry["author"][:255],
                "published_at": _published(entry["published_at"]),
                "companies": classification.get("companies", []),
            },
        )
        haystack = f"{title} {summary}".lower()
        selected_names = set(classification.get("themes", []))
        article.themes.set([theme for theme in themes if theme.name in selected_names or any(str(keyword).lower() in haystack for keyword in theme.keywords)])
        created += int(was_created)
    source.last_fetched_at = timezone.now()
    source.last_error = ""
    source.save(update_fields=["last_fetched_at", "last_error", "updated_at"])
    return {"created": created, "processed": len(entries[:100]), "discovery": "feed" if source.feed_url else "searxng_ai"}


def sync_industries_from_deals() -> int:
    """Ensure all distinct non-empty Deal.industry values exist in Industry table."""
    from deals.models import Deal
    from .models import Industry

    existing_names = set(Industry.objects.values_list("name", flat=True))
    raw_industries = (
        Deal.objects.exclude(industry__isnull=True)
        .exclude(industry="")
        .values_list("industry", flat=True)
        .distinct()
    )
    missing_names = {name.strip() for name in raw_industries if name and name.strip()} - existing_names
    if missing_names:
        objs = [Industry(name=name) for name in sorted(missing_names)]
        Industry.objects.bulk_create(objs, ignore_conflicts=True)
        return len(objs)
    return 0


def get_industry_deal_counts() -> dict[str, int]:
    """Return mapping of cleaned industry name to number of deals."""
    from deals.models import Deal
    from django.db.models import Count
    from django.db.models.functions import Trim

    rows = (
        Deal.objects.exclude(industry__isnull=True)
        .exclude(industry="")
        .annotate(clean_ind=Trim("industry"))
        .values("clean_ind")
        .annotate(cnt=Count("id"))
        .values_list("clean_ind", "cnt")
    )
    return dict(rows)


def pull_industry_news(industry, limit: int = 15):
    """Query SearXNG for recent industry news in India and store them."""
    from ai_orchestrator.services.search_provider import SearXNGProviderService
    from .models import IndustryNewsArticle

    provider = SearXNGProviderService()
    search_query = f'"{industry.name}" industry India market business news'
    try:
        results = provider.search_results(
            search_query,
            num_results=limit,
            context={"purpose": "industry news", "industry": industry.name, "geography": "India"},
        )
    except Exception:
        results = []

    for hit in results:
        url = hit.get("url")
        title = (hit.get("title") or "").strip()
        if not url or not title:
            continue
        summary = (hit.get("snippet") or hit.get("content") or "").strip()
        domain = urlparse(url).netloc.removeprefix("www.")
        pub_date_str = hit.get("published_date") or hit.get("publishedDate")
        published_at = _published(pub_date_str)

        IndustryNewsArticle.objects.update_or_create(
            industry=industry,
            url=url[:1000],
            defaults={
                "title": title[:600],
                "source_name": domain[:255],
                "summary": summary,
                "published_at": published_at,
            },
        )

    return list(industry.news_articles.all().order_by("-published_at", "-created_at")[:50])


def merge_industries(
    *,
    source_ids: list[str],
    target_id: str | None = None,
    target_name: str | None = None,
    user=None,
) -> dict:
    """
    Merges one or more source industries into a target industry.
    Updates all associated deals' industry field, records deal field provenance,
    migrates documents & news articles, combines notes/overview, and deletes source industries.
    """
    from django.db import transaction
    from deals.models import Deal
    from deals.services.field_provenance import record_deal_field_changes
    from .models import Industry, IndustryNewsArticle

    if not source_ids:
        raise ValueError("At least one source industry must be selected for merge.")

    sources = list(Industry.objects.filter(id__in=source_ids))
    if not sources:
        raise ValueError("No valid source industries found.")

    target = None
    if target_id:
        target = Industry.objects.filter(id=target_id).first()
        if not target:
            raise ValueError("Target industry not found.")
    elif target_name and target_name.strip():
        clean_target = target_name.strip()
        target, _ = Industry.objects.get_or_create(name=clean_target)
    else:
        raise ValueError("Either target_id or target_name must be provided.")

    sources = [s for s in sources if s.id != target.id]
    if not sources:
        raise ValueError("Cannot merge an industry into itself.")

    total_deals_updated = 0
    source_names = [s.name for s in sources]

    with transaction.atomic():
        for source in sources:
            deals = list(Deal.objects.filter(industry=source.name))
            for deal in deals:
                record_deal_field_changes(
                    deal,
                    {"industry": (source.name, target.name)},
                    source_type="HUMAN",
                    source_id=f"industry_merge:{source.name}->{target.name}"[:500],
                    changed_by=user if getattr(user, "is_authenticated", False) else None,
                )
            if deals:
                Deal.objects.filter(id__in=[d.id for d in deals]).update(industry=target.name)
                total_deals_updated += len(deals)

            source.documents.all().update(industry=target)

            for article in source.news_articles.all():
                if IndustryNewsArticle.objects.filter(industry=target, url=article.url).exists():
                    article.delete()
                else:
                    article.industry = target
                    article.save(update_fields=["industry"])

            if source.overview and source.overview.strip():
                if not target.overview:
                    target.overview = source.overview.strip()
                elif source.overview.strip() not in target.overview:
                    target.overview = f"{target.overview}\n\n---\nMerged from {source.name}:\n{source.overview.strip()}"

            if source.context and source.context.strip():
                if not target.context:
                    target.context = source.context.strip()
                elif source.context.strip() not in target.context:
                    target.context = f"{target.context}\n\n---\nMerged from {source.name}:\n{source.context.strip()}"

            source.delete()

        target.save(update_fields=["overview", "context", "updated_at"])

    return {
        "status": "success",
        "message": f"Successfully merged {len(source_names)} {('industry' if len(source_names) == 1 else 'industries')} ({', '.join(source_names)}) into '{target.name}'. {total_deals_updated} deals updated.",
        "target_industry_id": str(target.id),
        "target_name": target.name,
        "deals_updated": total_deals_updated,
    }

