import logging
import hashlib
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import urlparse

import requests
from django.conf import settings

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
        self.last_status = "not_run"

    def search_results(
        self,
        query: str,
        num_results: int = 5,
        *,
        aggregate_engines: bool = False,
    ) -> list[dict[str, Any]]:
        """Return normalized SearXNG results, keeping source metadata for grounding."""
        raw_results: list[dict[str, Any]] = []
        self.last_status = "searching"
        had_error = False
        engine_order = (
            [",".join(self.engines) if self.engines else None]
            if aggregate_engines
            else self._engine_order(query)
        )
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
                "published_date": str(item.get("publishedDate") or item.get("pubdate") or "").strip(),
                "query": query,
                "score": item.get("score"),
            })
        self.last_status = "completed" if results else ("failed" if had_error else "no_results")
        return results

    def _engine_order(self, query: str) -> list[str | None]:
        """Distribute queries across engines, retaining sequential fallbacks."""
        if not self.engines:
            return [None]
        digest = hashlib.sha256(query.strip().casefold().encode("utf-8")).digest()
        start = int.from_bytes(digest[:4], "big") % len(self.engines)
        return self.engines[start:] + self.engines[:start]

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
