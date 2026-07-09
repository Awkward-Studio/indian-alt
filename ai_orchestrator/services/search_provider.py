import requests
import logging
from django.conf import settings

logger = logging.getLogger(__name__)

class SearXNGProviderService:
    def __init__(self):
        self.base_url = getattr(settings, "SEARXNG_BASE_URL", "http://localhost:8081").rstrip("/")

    def search(self, query: str, num_results: int = 5) -> str:
        """Executes a search and returns formatted markdown context for an LLM."""
        try:
            logger.info(f"Querying SearXNG at {self.base_url} for: {query}")
            response = requests.get(
                f"{self.base_url}/search",
                params={"q": query, "format": "json"},
                timeout=15.0
            )
            response.raise_for_status()
            
            data = response.json()
            results = data.get("results", [])[:num_results]
            
            if not results:
                logger.warning(f"SearXNG returned no results for query: {query}")
                return "Web search returned no relevant results."

            context = "### Real-Time Web Search Results ###\n"
            for idx, r in enumerate(results, 1):
                context += f"Result {idx}:\n"
                context += f"- Title: {r.get('title')}\n"
                context += f"- Snippet: {r.get('content')}\n"
                context += f"- Source: {r.get('url')}\n\n"
            return context
        except Exception as e:
            logger.error(f"SearXNG search failed for '{query}': {e}")
            return "Web search failed or returned no results."
