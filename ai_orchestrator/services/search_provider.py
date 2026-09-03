import logging
import hashlib
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import urlparse

import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)


class SearXNGProviderService:
    def __init__(self):
        self.base_url = getattr(settings, "SEARXNG_BASE_URL", "http://localhost:8081").rstrip("/")
        self.timeout = float(getattr(settings, "SEARXNG_TIMEOUT", 15) or 15)
        self.max_results = int(getattr(settings, "SEARXNG_MAX_RESULTS", 30) or 30)
        self.search_workers = int(getattr(settings, "SEARXNG_SEARCH_WORKERS", 4) or 4)
        self.engines = [
            str(engine).strip()
            for engine in getattr(settings, "SEARXNG_ENGINES", [])
            if str(engine).strip()
        ]
        self.language = str(getattr(settings, "SEARXNG_LANGUAGE", "en-IN") or "en-IN").strip()
        self.cache_ttl = max(0, int(getattr(settings, "SEARXNG_CACHE_TTL", 300) or 0))
        self.last_status = "not_run"

    def search_results(
        self,
        query: str,
        num_results: int = 5,
        *,
        aggregate_engines: bool = False,
        engine_subset: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return normalized SearXNG results, keeping source metadata for grounding."""
        query = self.sanitize_query(query)
        if not query:
            self.last_status = "no_results"
            return []
        raw_results: list[dict[str, Any]] = []
        self.last_status = "searching"
        had_error = False
        selected_engines = engine_subset if engine_subset is not None else self.engines
        engine_order = (
            [",".join(selected_engines) if selected_engines else None]
            if aggregate_engines
            else self._engine_order(query)
        )
        cache_key = self._cache_key(query, num_results, engine_order)
        if self.cache_ttl:
            try:
                cached = cache.get(cache_key)
            except Exception:
                cached = None
            if isinstance(cached, list):
                self.last_status = "cache_hit"
                return cached
        for engine in engine_order:
            params = {"q": query, "format": "json", "language": self.language}
            if engine:
                params["engines"] = engine
            try:
                logger.info("Querying SearXNG at %s with engine %s for: %s", self.base_url, engine or "default", query)
                response = requests.get(
                    f"{self.base_url}/search",
                    params=params,
                    headers={
                        "X-Real-IP": "127.0.0.1",
                        "X-Forwarded-For": "127.0.0.1",
                    },
                    timeout=self.timeout,
                )
                response.raise_for_status()
                payload = response.json() or {}
                raw_results = payload.get("results", [])[: max(0, num_results)]
                if raw_results:
                    break
                engine_errors = payload.get("unresponsive_engines") or []
                logger.warning(
                    "SearXNG engine %s returned no results for %r%s",
                    engine or "default",
                    query,
                    f": {engine_errors}" if engine_errors else "",
                )
            except Exception as exc:
                had_error = True
                logger.warning("SearXNG engine %s failed for %r: %s", engine or "default", query, exc)

        results: list[dict[str, Any]] = []
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            title = str(item.get("title") or "").strip()
            snippet = str(item.get("content") or "").strip()
            if not url or not (title or snippet):
                continue
            results.append({
                "title": title,
                "snippet": snippet,
                "url": url,
                "engine": str(item.get("engine") or "").strip(),
                "engines": [str(engine).strip() for engine in (item.get("engines") or []) if str(engine).strip()],
                "published_date": str(item.get("publishedDate") or item.get("pubdate") or "").strip(),
                "query": query,
                "score": item.get("score"),
            })
        self.last_status = "completed" if results else ("failed" if had_error else "no_results")
        if results and self.cache_ttl:
            try:
                cache.set(cache_key, results, timeout=self.cache_ttl)
            except Exception:
                pass
        return results

    def _cache_key(self, query: str, num_results: int, engine_order: list[str | None]) -> str:
        material = "|".join([
            self.base_url,
            self.language,
            str(num_results),
            ",".join(str(engine or "default") for engine in engine_order),
            query.casefold(),
        ])
        return f"searxng:search:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"

    @staticmethod
    def sanitize_query(value: Any, *, max_length: int = 320) -> str:
        """Bound outbound queries and remove common accidental private identifiers."""
        query = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or ""))
        query = re.sub(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", " ", query, flags=re.IGNORECASE)
        query = re.sub(r"(?<!\d)(?:\+?\d[\d ()-]{8,}\d)(?!\d)", " ", query)
        query = re.sub(
            r"(?i)(?:\b(?:INR|USD|EUR|GBP|Rs\.?)\s*[₹$€£]?|[₹$€£])\s*\d[\d,.]*\s*(?:crore|cr|lakh|million|billion|mn|bn)?\b",
            " ",
            query,
        )
        query = re.sub(r"(?<!\w)\d+(?:\.\d+)?%(?!\w)", " ", query)
        return re.sub(r"\s+", " ", query).strip()[:max_length]

    def _engine_order(self, query: str) -> list[str | None]:
        """Distribute queries across engines, retaining sequential fallbacks."""
        if not self.engines:
            return [None]
        digest = hashlib.sha256(query.strip().casefold().encode("utf-8")).digest()
        start = int.from_bytes(digest[:4], "big") % len(self.engines)
        return self.engines[start:] + self.engines[:start]

    def engine_subset_for_query(self, query: str, *, limit: int = 3) -> list[str]:
        """Choose a small provider group suited to one planned query."""
        lowered = str(query or "").casefold()
        if re.search(r"\b(news|latest|recent|today|update|funding|investment|acquisition|regulatory|regulation|licen[cs]e|rbi)\b", lowered):
            anchors = ["duckduckgo news", "bing news"]
            optional = ["brave.news", "mwmbl"]
        elif re.search(r"\b(what is|who is|overview|profile|founded|founder|headquarter|business model|history)\b", lowered):
            anchors = ["duckduckgo web", "bing"]
            optional = ["brave", "wikipedia", "wikidata"]
        else:
            anchors = ["duckduckgo web", "bing"]
            optional = ["brave", "mwmbl", "privacywall"]

        configured = {engine.casefold(): engine for engine in self.engines}
        selected = [configured[name.casefold()] for name in anchors if name.casefold() in configured]
        available_optional = [configured[name.casefold()] for name in optional if name.casefold() in configured]
        if available_optional and len(selected) < limit:
            digest = hashlib.sha256(str(query or "").strip().casefold().encode("utf-8")).digest()
            start = int.from_bytes(digest[:4], "big") % len(available_optional)
            selected.extend((available_optional[start:] + available_optional[:start])[: limit - len(selected)])
        available = [*selected, *[engine for engine in self.engines if engine not in selected]]
        if not available:
            return []
        return available[:limit]

    def _engine_subset(self, query: str, *, limit: int = 3) -> list[str]:
        """Backward-compatible alias for callers outside the shared provider."""
        return self.engine_subset_for_query(query, limit=limit)

    def search_many(
        self,
        queries: list[str],
        *,
        results_per_query: int = 5,
        max_results: int | None = None,
    ) -> list[dict[str, Any]]:
        """Run independent searches concurrently and deduplicate their source URLs."""
        unique_queries = list(dict.fromkeys(query.strip() for query in queries if query and query.strip()))
        if not unique_queries:
            self.last_status = "no_results"
            return []

        results_by_query: dict[str, list[dict[str, Any]]] = {}
        worker_count = max(1, min(self.search_workers, len(unique_queries)))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_queries = {
                executor.submit(
                    self.search_results,
                    query,
                    results_per_query,
                    aggregate_engines=True,
                    engine_subset=self.engine_subset_for_query(query),
                ): query
                for query in unique_queries
            }
            for future in as_completed(future_queries):
                query = future_queries[future]
                try:
                    results_by_query[query] = future.result()
                except Exception as exc:
                    logger.warning("SearXNG query failed for %r: %s", query, exc)
                    results_by_query[query] = []

        combined: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        result_limit = max_results if max_results is not None else self.max_results
        result_groups = [results_by_query.get(query, []) for query in unique_queries]
        max_group_size = max((len(group) for group in result_groups), default=0)
        for result_index in range(max_group_size):
            for group in result_groups:
                if result_index >= len(group):
                    continue
                result = group[result_index]
                if self._is_low_value_navigation_result(result):
                    continue
                key = self.normalize_url(result.get("url"))
                if not key or key in seen_urls:
                    continue
                seen_urls.add(key)
                combined.append(result)
                if len(combined) >= result_limit:
                    self.last_status = "completed"
                    return combined
        self.last_status = "completed" if combined else "no_results"
        return combined

    @staticmethod
    def _is_low_value_navigation_result(result: dict[str, Any]) -> bool:
        """Exclude account/navigation pages that do not provide research evidence."""
        haystack = f"{result.get('title') or ''} {result.get('url') or ''}".casefold()
        if re.search(r"(?:\bsign[ -]?up\b|\blog[ -]?in\b|\bdashboard\b|\bonboarding\b|/login(?:[/?#]|$)|/signup(?:[/?#]|$))", haystack):
            return True

        query = str(result.get("query") or "").casefold()
        research_terms = r"\b(news|latest|recent|development|regulatory|regulation|funding|investment|valuation|competitor|market|risk|lawsuit|acquisition)\b"
        if not re.search(research_terms, query):
            return False
        parsed = urlparse(str(result.get("url") or ""))
        path = parsed.path.rstrip("/")
        title = str(result.get("title") or "").casefold()
        return not path or bool(re.search(r"\b(payment solution|payment gateway|customer care|support)\b", title))

    def format_context(
        self,
        results: list[dict[str, Any]],
        *,
        heading: str = "Real-Time Web Search Evidence",
        include_query: bool = True,
        include_page_content: bool = False,
    ) -> str:
        if not results:
            return "Web search returned no relevant results."
        lines = [f"### {heading} ###"]
        for index, result in enumerate(results, 1):
            item_lines = [
                f"[S{index}] {result.get('title') or 'Untitled result'}",
                f"Snippet: {result.get('snippet') or 'No snippet available'}",
            ]
            if include_page_content and result.get("page_content"):
                item_lines.append(f"Page excerpt: {result.get('page_content')}")
            item_lines.extend([f"URL: {result.get('url')}", ""])
            if include_query:
                item_lines.insert(1, f"Query: {result.get('query') or 'N/A'}")
            lines.extend(item_lines)
        return "\n".join(lines).strip()

    def search(self, query: str, num_results: int = 5) -> str:
        """Backward-compatible formatted search context for existing callers."""
        return self.format_context(self.search_results(query, num_results))

    @staticmethod
    def normalize_url(value: Any) -> str:
        return str(value or "").strip().rstrip("/").casefold()
