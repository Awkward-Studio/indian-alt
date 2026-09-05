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
        }, timeout=180)
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
