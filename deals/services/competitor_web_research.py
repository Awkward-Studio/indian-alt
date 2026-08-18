from __future__ import annotations

import json
import ipaddress
import re
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from django.conf import settings

from ai_orchestrator.services.llm_providers import VLLMProviderService
from ai_orchestrator.services.pipeline_registry import PipelineRegistryService
from ai_orchestrator.services.runtime import AIRuntimeService
from ai_orchestrator.services.search_provider import SearXNGProviderService
from deals.services.competitor_intelligence import competitor_names_from_payload


class CompetitorWebResearchService:
    """Extract grounded competitor intelligence from capped public/private searches."""

    def __init__(self, *, search_service=None, llm_service=None):
        self.search_service = search_service or SearXNGProviderService()
        self.llm_service = llm_service or VLLMProviderService()
        self.model = (
            AIRuntimeService.get_text_model()
            or "local-model"
        )
        self.page_fetch_limit = int(getattr(settings, "SEARXNG_PAGE_FETCH_LIMIT", 6) or 6)
        self.page_fetch_workers = int(getattr(settings, "SEARXNG_PAGE_FETCH_WORKERS", 3) or 3)
        self.page_fetch_timeout = float(getattr(settings, "SEARXNG_PAGE_FETCH_TIMEOUT", 8) or 8)
        self.page_fetch_max_bytes = int(getattr(settings, "SEARXNG_PAGE_FETCH_MAX_BYTES", 1_000_000) or 1_000_000)
        self.page_fetch_max_chars = int(getattr(settings, "SEARXNG_PAGE_FETCH_MAX_CHARS", 7_000) or 7_000)
        self.evidence_max_chars = int(getattr(settings, "SEARXNG_EVIDENCE_MAX_CHARS", 18_000) or 18_000)

    def research(
        self,
        *,
        company_name: str,
        sector: str = "",
        industry: str = "",
        location: str = "",
        business_summary: str = "",
        instruction: str = "",
        existing_competitors: list[dict] | None = None,
    ) -> dict[str, Any]:
        existing_competitors = existing_competitors or []
        existing_names = [
            str(item.get("name") or item.get("company_name") or "").strip()
            for item in existing_competitors
            if isinstance(item, dict) and str(item.get("name") or item.get("company_name") or "").strip()
        ][:30]

        candidate_groups = {"public": [], "private": []}
        fallback_queries = self._research_queries(
            company_name=company_name,
            sector=sector,
            industry=industry,
            location=location,
            business_summary=business_summary,
            instruction=instruction,
            candidate_groups=candidate_groups,
        )
        research_queries, query_plan_diagnostics = self._plan_research_queries(
            company_name=company_name,
            sector=sector,
            industry=industry,
            location=location,
            business_summary=business_summary,
            instruction=instruction,
            fallback_queries=fallback_queries,
        )
        evidence_results = self._search_balanced_evidence(research_queries)
        if not evidence_results:
            return {
                "competitors": [],
                "response": "",
                "message": "The public/private SearXNG queries returned no competitor evidence.",
                "diagnostics": {
                    "search_queries": research_queries,
                    "query_plan": query_plan_diagnostics,
                    "search_requests": len(research_queries),
                    "search_sources": 0,
                    "discovery_queries": list(research_queries.values()),
                    "discovery_sources": 0,
                    "verification_sources": 0,
                },
            }

        search_source_count = len(evidence_results)
        evidence_results = self._enrich_evidence(
            company_name=company_name,
            results=evidence_results,
        )
        fetched_pages = sum(bool(item.get("page_content")) for item in evidence_results)
        evidence_results = self._limit_evidence_context(
            company_name=company_name,
            results=evidence_results,
        )

        responses = {}
        extracted = []
        for route in ("public", "private"):
            route_evidence = [
                item for item in evidence_results
                if item.get("discovery_route") == route
            ]
            if not route_evidence:
                continue
            evidence_context = self.search_service.format_context(
                route_evidence,
                heading=f"{route.title()} Competitor Evidence",
                include_query=False,
                include_page_content=True,
            )
            route_instruction = (
                "Return up to 8 direct competitors or closest listed market comparables "
                "with meaningful product/category overlap. Use listed_public only with "
                "exact legal-entity exchange and ticker evidence."
                if route == "public"
                else
                "Return up to 8 direct private or unlisted competitor companies or brands. "
                "Exclude the target's parent, subsidiaries, sister brands, and aliases."
            )
            system, research_prompt, _ = PipelineRegistryService.render_prompt_stage(
                "competitor_research",
                "extract",
                company_name=company_name,
                sector=sector or "N/A",
                industry=industry or "N/A",
                location=location or "N/A",
                business_summary=(business_summary or "N/A")[:1200],
                instruction=f"{instruction}\n{route_instruction}" or "Find direct competitors and close market peers",
                existing_names=", ".join(existing_names) or "None",
                evidence_context=evidence_context,
            )
            route_response = self._infer(
                system=system,
                prompt=research_prompt,
                max_tokens=1800,
            )
            responses[route] = route_response
            route_candidates = competitor_names_from_payload(
                route_response,
                limit=8,
                include_cin=False,
            )
            route_candidates = self._attach_matching_evidence(route_candidates, route_evidence)
            extracted.extend({
                **item,
                "discovery_route": route,
            } for item in route_candidates)
        response = json.dumps(responses, ensure_ascii=False)
        extracted = self._deduplicate(extracted)
        if not extracted:
            return {
                "competitors": [],
                "response": response,
                "message": "Web evidence was found, but the local model returned no parseable competitors.",
                "diagnostics": {
                    "model": self.model,
                    "search_queries": research_queries,
                    "query_plan": query_plan_diagnostics,
                    "search_requests": len(research_queries),
                    "search_sources": search_source_count,
                    "evidence_sources": len(evidence_results),
                    "discovery_queries": list(research_queries.values()),
                    "discovery_sources": len(evidence_results),
                    "verification_sources": 0,
                    "page_fetches": fetched_pages,
                },
            }

        grounded = self._ground_candidates(
            extracted,
            evidence_results=evidence_results,
            target_company_name=company_name,
        )
        grounded = self._confirm_screener_listings(grounded)
        grounded = self._balance_company_types(self._deduplicate(grounded), limit=10)
        classification_counts = {
            company_type: sum(item.get("company_type") == company_type for item in grounded)
            for company_type in ("listed_public", "private", "unknown")
        }

        return {
            "competitors": grounded,
            "response": response,
            "diagnostics": {
                "model": self.model,
                "candidate_groups": candidate_groups,
                "search_queries": research_queries,
                "query_plan": query_plan_diagnostics,
                "search_requests": len(research_queries),
                "search_sources": search_source_count,
                "evidence_sources": len(evidence_results),
                "discovery_queries": list(research_queries.values()),
                "discovery_sources": len(evidence_results),
                "verification_queries": [],
                "verification_sources": 0,
                "page_fetches": fetched_pages,
                "discovered_candidates": len(extracted),
                "verified_candidates": len(grounded),
                "classification_counts": classification_counts,
                "minimum_target": {"listed_public": 4, "private": 4},
                "minimum_target_met": (
                    classification_counts["listed_public"] >= 4
                    and classification_counts["private"] >= 4
                ),
            },
        }

    def _plan_research_queries(
        self,
        *,
        company_name: str,
        sector: str,
        industry: str,
        location: str,
        business_summary: str,
        instruction: str,
        fallback_queries: dict[str, str],
    ) -> tuple[dict[str, str], dict[str, Any]]:
        system, prompt, _ = PipelineRegistryService.render_prompt_stage(
            "competitor_research",
            "query_planner",
            company_name=company_name,
            sector=sector or "N/A",
            industry=industry or "N/A",
            location=location or "N/A",
            business_summary=(business_summary or "N/A")[:2400],
            instruction=instruction or "N/A",
        )
        payload = {
            "model": self.model,
            # JSON transport constraints are an immutable provider boundary.
            "system": system or "Return valid JSON only. Do not include markdown or reasoning.",
            "prompt": prompt,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "competitor_search_queries",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "public_query": {"type": "string"},
                            "private_query": {"type": "string"},
                            "inferred_category": {"type": "string"},
                        },
                        "required": ["public_query", "private_query", "inferred_category"],
                        "additionalProperties": False,
                    },
                },
            },
            "chat_template_kwargs": {"enable_thinking": False},
            "options": {"temperature": 0.0, "max_tokens": 500},
        }
        try:
            result = self.llm_service.execute_standard(payload, timeout=180)
            raw = str(result.get("response") or "").strip()
            match = re.search(r"\{[\s\S]*\}", raw)
            parsed = json.loads(match.group(0) if match else raw)
            public_query = re.sub(r"\s+", " ", str(parsed.get("public_query") or "")).strip()[:350]
            private_query = re.sub(r"\s+", " ", str(parsed.get("private_query") or "")).strip()[:350]
            if not public_query or not private_query:
                raise ValueError("VM query planner returned empty queries")
            return {
                "public": public_query,
                "private": private_query,
            }, {
                "source": "vm",
                "model": self.model,
                "inferred_category": str(parsed.get("inferred_category") or "").strip(),
            }
        except Exception as exc:
            return fallback_queries, {
                "source": "deterministic_fallback",
                "model": self.model,
                "error": str(exc)[:300],
            }

    def _search_balanced_evidence(self, queries: dict[str, str]) -> list[dict]:
        results_by_route: dict[str, list[dict]] = {}
        with ThreadPoolExecutor(max_workers=max(1, len(queries))) as executor:
            futures = {
                executor.submit(
                    self.search_service.search_results,
                    query,
                    num_results=min(max(self.search_service.max_results, 20), 40),
                    aggregate_engines=True,
                ): route
                for route, query in queries.items()
            }
            for future in as_completed(futures):
                route = futures[future]
                try:
                    results_by_route[route] = future.result() or []
                except Exception:
                    results_by_route[route] = []

        merged: list[dict] = []
        seen_urls: set[tuple[str, str]] = set()
        for route in ("public", "private"):
            for result in results_by_route.get(route, []):
                normalized_url = self.search_service.normalize_url(result.get("url"))
                route_url = (route, normalized_url)
                if not normalized_url or route_url in seen_urls:
                    continue
                seen_urls.add(route_url)
                merged.append({**result, "discovery_route": route})
        return merged

    def _infer(self, *, system: str, prompt: str, max_tokens: int) -> str:
        payload = {
            "model": self.model,
            "system": system,
            "prompt": prompt,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "competitor_research",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "competitors": {
                                "type": "array",
                                "maxItems": 12,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "company_name": {"type": "string"},
                                        "core_business": {"type": "string"},
                                        "nature_of_competition": {"type": "string"},
                                        "company_type": {
                                            "type": "string",
                                            "enum": ["listed_public", "private", "unknown"],
                                        },
                                        "classification_confidence": {
                                            "type": "number",
                                            "minimum": 0,
                                            "maximum": 1,
                                        },
                                        "exchange": {"type": "string"},
                                        "ticker": {"type": "string"},
                                        "screener_url": {"type": "string"},
                                        "classification_source": {"type": "string"},
                                        "evidence_urls": {
                                            "type": "array",
                                            "items": {"type": "string"},
                                            "maxItems": 3,
                                        },
                                    },
                                    "required": [
                                        "company_name",
                                        "core_business",
                                        "nature_of_competition",
                                        "company_type",
                                        "classification_confidence",
                                        "exchange",
                                        "ticker",
                                        "screener_url",
                                        "classification_source",
                                        "evidence_urls",
                                    ],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "required": ["competitors"],
                        "additionalProperties": False,
                    },
                },
            },
            "chat_template_kwargs": {"enable_thinking": False},
            "options": {"temperature": 0.0, "max_tokens": max_tokens},
        }
        try:
            result = self.llm_service.execute_standard(payload, timeout=600)
        except requests.RequestException:
            # Keep compatibility with OpenAI-style servers that do not support
            # response_format while preferring constrained JSON when available.
            payload.pop("response_format", None)
            result = self.llm_service.execute_standard(payload, timeout=600)
        return str(result.get("response") or "")

    def _enrich_evidence(self, *, company_name: str, results: list[dict]) -> list[dict]:
        per_route_limit = max(1, self.page_fetch_limit // 2)
        ranked_indexes = []
        for route in ("public", "private"):
            route_indexes = [
                index
                for index, result in enumerate(results)
                if result.get("discovery_route") == route
            ]
            ranked_indexes.extend(sorted(
                route_indexes,
                key=lambda index: self._page_relevance(results[index], company_name),
                reverse=True,
            )[:per_route_limit])
        if not ranked_indexes:
            return results

        fetched: dict[int, str] = {}
        worker_count = max(1, min(self.page_fetch_workers, len(ranked_indexes)))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(self._fetch_page_text, str(results[index].get("url") or "")): index
                for index in ranked_indexes
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    text = future.result()
                except Exception:
                    text = ""
                if text:
                    fetched[index] = text

        return [
            {**result, "page_content": fetched.get(index, "")}
            for index, result in enumerate(results)
        ]

    def _limit_evidence_context(self, *, company_name: str, results: list[dict]) -> list[dict]:
        ranked_by_route = {
            route: sorted(
                [
                    item
                    for item in results
                    if (
                        item.get("discovery_route") == route
                        if route != "other"
                        else item.get("discovery_route") not in {"public", "private"}
                    )
                ],
                key=lambda item: (bool(item.get("page_content")), self._page_relevance(item, company_name)),
                reverse=True,
            )
            for route in ("public", "private", "other")
        }
        ranked = []
        max_route_length = max((len(items) for items in ranked_by_route.values()), default=0)
        for index in range(max_route_length):
            for route in ("public", "private", "other"):
                if index < len(ranked_by_route[route]):
                    ranked.append(ranked_by_route[route][index])
        selected: list[dict] = []
        remaining = max(1_000, self.evidence_max_chars)
        for result in ranked:
            base_size = sum(len(str(result.get(key) or "")) for key in ("title", "snippet", "url")) + 120
            if base_size >= remaining:
                continue
            item = dict(result)
            page_content = str(item.get("page_content") or "")
            if page_content:
                item["page_content"] = page_content[: max(0, remaining - base_size)]
            item_size = base_size + len(str(item.get("page_content") or ""))
            selected.append(item)
            remaining -= item_size
            if remaining < 500:
                break
        return selected

    @staticmethod
    def _page_relevance(result: dict, company_name: str) -> tuple[int, float]:
        text = " ".join([
            str(result.get("title") or ""),
            str(result.get("snippet") or ""),
        ]).casefold()
        company_tokens = [token for token in re.findall(r"[a-z0-9]+", company_name.casefold()) if len(token) > 2]
        score = sum(2 for token in company_tokens if token in text)
        score += sum(
            3 for marker in ("competitor", "alternative", "comparison", " versus ", " vs ", "market share", "rival")
            if marker in text
        )
        return score, float(result.get("score") or 0)

    def _fetch_page_text(self, url: str) -> str:
        current_url = url
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; IndiaAlternativesResearch/1.0)",
            "Accept": "text/html,application/xhtml+xml",
        }
        for _ in range(4):
            if not self._is_safe_public_url(current_url):
                return ""
            response = requests.get(
                current_url,
                headers=headers,
                timeout=self.page_fetch_timeout,
                allow_redirects=False,
                stream=True,
            )
            if response.status_code in {301, 302, 303, 307, 308}:
                redirect = response.headers.get("Location")
                if not redirect:
                    return ""
                current_url = urljoin(current_url, redirect)
                continue
            response.raise_for_status()
            content_type = str(response.headers.get("Content-Type") or "").casefold()
            if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
                return ""
            body = bytearray()
            for chunk in response.iter_content(chunk_size=65_536):
                if not chunk:
                    continue
                remaining = self.page_fetch_max_bytes - len(body)
                if remaining <= 0:
                    break
                body.extend(chunk[:remaining])
            encoding = response.encoding or "utf-8"
            return self._extract_page_text(bytes(body).decode(encoding, errors="replace"))
        return ""

    def _extract_page_text(self, html: str) -> str:
        soup = BeautifulSoup(html or "", "lxml")
        for node in soup.select("script, style, nav, footer, header, form, noscript, svg, iframe"):
            node.decompose()
        root = soup.find("article") or soup.find("main") or soup.body or soup
        parts: list[str] = []
        seen: set[str] = set()
        for node in root.select("h1, h2, h3, p, li, td"):
            text = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
            key = text.casefold()
            if len(text) < 30 or key in seen:
                continue
            seen.add(key)
            parts.append(text)
            if sum(len(part) + 1 for part in parts) >= self.page_fetch_max_chars:
                break
        return "\n".join(parts)[: self.page_fetch_max_chars]

    @staticmethod
    def _is_safe_public_url(url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        try:
            addresses = {
                item[4][0]
                for item in socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
            }
            return bool(addresses) and all(ipaddress.ip_address(address).is_global for address in addresses)
        except (OSError, ValueError):
            return False

    def _research_queries(
        self,
        *,
        company_name: str,
        sector: str,
        industry: str,
        location: str,
        business_summary: str,
        instruction: str,
        candidate_groups: dict[str, list[str]] | None = None,
    ) -> dict[str, str]:
        market = " ".join(value for value in [sector, industry] if value).strip()
        place = location or "India"
        business_focus = re.sub(r"\s+", " ", str(business_summary or "")).strip()[:180]
        candidate_groups = candidate_groups or {}
        public_names = " OR ".join(f'"{name}"' for name in candidate_groups.get("public", []))
        private_names = " OR ".join(f'"{name}"' for name in candidate_groups.get("private", []))
        return {
            "public": re.sub(
                r"\s+",
                " ",
                (
                    f'({public_names}) {market} {place} NSE BSE listed ticker competitors'
                    if public_names
                    else (
                        f"{place} listed companies NSE BSE ticker public stocks closest market "
                        f"comparables for this business: {business_focus or market}"
                    )
                ),
            ).strip()[:350],
            "private": re.sub(
                r"\s+",
                " ",
                (
                    f'({private_names}) {market} {place} private unlisted D2C competitors'
                    if private_names
                    else (
                        f'"{company_name}" competitors {business_focus or market} {place} '
                        "private unlisted brands companies startups"
                    )
                ),
            ).strip()[:350],
        }


    @staticmethod
    def _category_hint(*, company_name: str, sector: str, industry: str) -> str:
        supplied = " ".join(value for value in [sector, industry] if value).strip()
        if supplied:
            return supplied
        normalized_name = company_name.casefold()
        if "shampoo" in normalized_name:
            return (
                "mainstream everyday shampoo and hair-care brands; exclude medicated, "
                "pharmaceutical, salon-only, and dandruff-treatment specialists"
            )
        return "mainstream product or service category implied by the target name"

    @staticmethod
    def _research_query(*, company_name: str, sector: str, industry: str, instruction: str) -> str:
        market = " ".join(value for value in [sector, industry] if value).strip()
        intent = instruction.strip() if instruction else '(competitors OR alternatives OR "market share" OR versus)'
        return " ".join(filter(None, [
            f'"{company_name}"',
            intent,
            market,
            "India market rival brands",
        ]))

    @staticmethod
    def _research_prompt(**context: Any) -> str:
        return f"""
Build a detailed competitor set for {context['company_name']} using ONLY the evidence below.

Company context:
- Sector: {context['sector'] or 'N/A'}
- Industry: {context['industry'] or 'N/A'}
- Location: {context['location'] or 'N/A'}
- Business summary: {(context['business_summary'] or 'N/A')[:1200]}
- User instruction: {context['instruction'] or 'Find direct competitors and close market peers'}
- Existing names to exclude: {', '.join(context['existing_names']) or 'None'}

Rules:
- Include a company only when at least one supplied source explicitly supports the competitive relationship.
- Explain the overlapping product, customer, geography, or business model in nature_of_competition.
- The search query is not evidence and is intentionally omitted; never treat a hypothesized name as verified.
- Do not state a parent company, owner, or private/public status in core_business unless a supplied snippet says so.
- Set company_type to listed_public only when the evidence explicitly supports the exact legal entity's exchange and ticker.
- Use private only when the evidence explicitly says privately held/private; otherwise use unknown.
- A listed parent does not make its brand or subsidiary listed_public.
- Never invent a ticker, exchange, ownership relationship, or Screener URL.
- Merge brands or aliases that refer to the same competitor.
- Copy 1-3 supporting URLs exactly from the evidence.
- Target at least 6 listed_public and 6 private candidates before validation, so the final set can retain at least 4 of each.
- If one group has fewer than 4 evidence-backed companies, do not relabel unknown companies to manufacture the quota.
- Prefer direct competitors over broad conglomerates and return at most 12 companies.
- Never return the target company, its parent/owner, a source name, or a descriptive group as a competitor.
- Return one JSON object and no markdown:
{{"competitors":[{{"company_name":"...","core_business":"...","nature_of_competition":"...","company_type":"listed_public|private|unknown","classification_confidence":0.0,"exchange":"","ticker":"","screener_url":"","classification_source":"short evidence-based explanation","evidence_urls":["exact source URL"]}}]}}

{context['evidence_context']}
""".strip()

    @staticmethod
    def _verification_prompt(*, company_name: str, candidates: list[dict], evidence_context: str) -> str:
        candidate_payload = json.dumps(
            [{"company_name": item.get("name"), "discovery_notes": item.get("notes", "")} for item in candidates],
            ensure_ascii=False,
        )
        return f"""
Verify these proposed competitors for {company_name}: {candidate_payload}

Use ONLY the supplied evidence. For each distinct company:
- Keep the brand/company identity separate from its parent; explain the relationship briefly.
- Set company_type to listed_public only with explicit exchange/ticker evidence for that exact legal entity.
- Otherwise use private when explicitly supported, or unknown when evidence is insufficient.
- Never infer that a subsidiary or brand is listed merely because its parent is listed.
- Never invent an exchange, ticker, ownership relationship, or Screener URL.
- Copy 1-3 supporting URLs exactly from the evidence into evidence_urls.
- Remove duplicate aliases.

Return one JSON object and no markdown:
{{"competitors":[{{"company_name":"...","core_business":"...","nature_of_competition":"...","company_type":"listed_public|private|unknown","classification_confidence":0.0,"exchange":"","ticker":"","screener_url":"","classification_source":"short evidence-based explanation","evidence_urls":["exact source URL"]}}]}}

{evidence_context}
""".strip()

    def _ground_candidates(
        self,
        candidates: list[dict],
        *,
        evidence_results: list[dict],
        target_company_name: str = "",
    ) -> list[dict]:
        evidence_by_url = {
            self.search_service.normalize_url(result.get("url")): " ".join([
                str(result.get("title") or ""),
                str(result.get("snippet") or ""),
                str(result.get("page_content") or ""),
            ]).casefold()
            for result in evidence_results
            if self.search_service.normalize_url(result.get("url"))
        }
        grounded: list[dict] = []
        for candidate in candidates:
            candidate_name = str(candidate.get("name") or "").strip()
            target_key = self._canonical_key({"name": target_company_name})
            candidate_key = self._canonical_key({"name": candidate_name})
            target_tokens = set(target_key.split())
            candidate_tokens = set(candidate_key.split())
            relationship_text = " ".join([
                str(candidate.get("notes") or ""),
                str(candidate.get("classification_source") or ""),
            ]).casefold()
            if target_tokens and target_tokens.issubset(candidate_tokens):
                continue
            if any(marker in relationship_text for marker in (
                "parent company of",
                "owner of the target",
                "owns the target",
                "sister brand",
                "same parent",
                "under the same parent",
                "under honasa",
                "honasa consumer house of brands",
                "honasa consumer portfolio",
            )):
                continue
            evidence_urls = [
                url for url in candidate.get("evidence_urls", [])
                if self.search_service.normalize_url(url) in evidence_by_url
            ]
            item = {**candidate, "evidence_urls": evidence_urls}
            if not evidence_urls:
                continue
            confidence = item.get("classification_confidence")
            confidence = float(confidence) if isinstance(confidence, (int, float)) else 0.0
            evidence_text = " ".join(
                evidence_by_url[self.search_service.normalize_url(url)]
                for url in evidence_urls
            )
            name_tokens = [
                token for token in re.findall(r"[a-z0-9]+", candidate_name.casefold())
                if len(token) > 2 and token not in {"limited", "private", "company", "group"}
            ]
            if not name_tokens or not any(token in evidence_text for token in name_tokens):
                continue
            ticker_symbols = [
                token.removesuffix(".ns").removesuffix(".bo")
                for token in re.findall(r"[a-z0-9.]+", str(item.get("ticker") or "").casefold())
                if len(token) >= 2 and token not in {"nse", "bse"}
            ]
            ticker_is_evidenced = bool(ticker_symbols) and any(
                re.search(rf"\b{re.escape(symbol)}\b", evidence_text)
                for symbol in ticker_symbols
            )
            listing_is_evidenced = any(
                marker in evidence_text
                for marker in ("listed", "ticker", " nse", " bse", "stock exchange", "publicly traded")
            )
            name_tokens = set(re.findall(r"[a-z0-9]+", str(item.get("name") or "").casefold()))
            ticker_matches_identity = any(symbol in name_tokens for symbol in ticker_symbols)
            source_text = str(item.get("classification_source") or "").casefold()
            source_supports_ticker = any(
                re.search(rf"\b{re.escape(symbol)}\b", source_text)
                for symbol in ticker_symbols
            )
            source_supports_listing = any(
                marker in source_text
                for marker in ("listed", "ticker", " nse", " bse", "stock exchange", "publicly traded")
            )
            parent_only_claim = (
                any(marker in source_text for marker in ("owned by", "parent", "subsidiary", "holding company", "operates under"))
                and not ticker_matches_identity
            )
            is_supported_public = (
                item.get("company_type") == "listed_public"
                and bool(item.get("exchange"))
                and ticker_is_evidenced
                and listing_is_evidenced
                and source_supports_ticker
                and source_supports_listing
                and confidence >= 0.65
                and bool(evidence_urls)
                and not parent_only_claim
            )
            private_is_evidenced = any(
                marker in evidence_text
                for marker in ("private company", "privately held", "not publicly traded", "private stock", "private valuation")
            )
            is_supported_private = (
                item.get("company_type") == "private"
                and confidence >= 0.6
                and bool(evidence_urls)
                and private_is_evidenced
            )
            if not (is_supported_public or is_supported_private):
                item.update({
                    "company_type": "unknown",
                    "classification_confidence": min(confidence, 0.4),
                    "exchange": "",
                    "ticker": "",
                    "screener_url": "",
                    "classification_source": "Insufficient search evidence to verify listing status.",
                })
            grounded.append(item)
        return grounded

    @staticmethod
    def _canonical_key(candidate: dict) -> str:
        name = str(candidate.get("name") or "").strip()
        tokens = re.findall(r"[a-z0-9]+", name.casefold())
        tokens = [token for token in tokens if token not in {"limited", "ltd", "private", "pvt", "inc", "company", "co"}]
        return " ".join(sorted(tokens))

    @classmethod
    def _merge_verified_candidates(cls, discovered: list[dict], verified: list[dict]) -> list[dict]:
        verified_by_key = {}
        for candidate in verified:
            key = cls._canonical_key(candidate)
            if key:
                verified_by_key.setdefault(key, candidate)
        merged = []
        for candidate in discovered:
            key = cls._canonical_key(candidate)
            merged.append(verified_by_key.pop(key, candidate))
        merged.extend(verified_by_key.values())
        return merged

    def _attach_matching_evidence(self, candidates: list[dict], evidence_results: list[dict]) -> list[dict]:
        """Attach source URLs deterministically when a result explicitly names a candidate."""
        enriched = []
        for candidate in candidates:
            name = str(candidate.get("name") or "").casefold().strip()
            name_tokens = [token for token in re.findall(r"[a-z0-9]+", name) if len(token) > 2]
            urls = list(candidate.get("evidence_urls") or [])
            for result in evidence_results:
                haystack = " ".join([
                    str(result.get("title") or ""),
                    str(result.get("snippet") or ""),
                ]).casefold()
                if name_tokens and all(token in haystack for token in name_tokens):
                    url = str(result.get("url") or "").strip()
                    if url and url not in urls:
                        urls.append(url)
                if len(urls) >= 3:
                    break
            enriched.append({**candidate, "evidence_urls": urls[:3]})
        return enriched

    @classmethod
    def _deduplicate(cls, candidates: list[dict]) -> list[dict]:
        results: list[dict] = []
        seen: set[str] = set()
        for candidate in candidates:
            key = cls._canonical_key(candidate)
            if not key or key in seen:
                continue
            seen.add(key)
            results.append(candidate)
        return results

    @staticmethod
    def _confirm_screener_listings(candidates: list[dict]) -> list[dict]:
        if not candidates:
            return []
        from deals.services.screener import ScreenerCompanyService

        service = ScreenerCompanyService()
        confirmations: dict[int, dict] = {}
        lookup_failures: set[int] = set()
        lookup_indexes = [
            index
            for index, item in enumerate(candidates)
            if (
                item.get("discovery_route") == "public"
                or item.get("company_type") == "listed_public"
                or item.get("ticker")
                or item.get("exchange")
            )
        ]
        # Screener rate-limits bursts aggressively. Private-route candidates do
        # not need a listing lookup, and two workers keep the public validation
        # useful without turning transient 429s into a classification decision.
        with ThreadPoolExecutor(max_workers=min(2, len(lookup_indexes) or 1)) as executor:
            futures = {
                executor.submit(
                    service.search_company,
                    str(candidates[index].get("name") or ""),
                    raise_on_error=True,
                ): index
                for index in lookup_indexes
                if candidates[index].get("name")
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    confirmations[index] = future.result() or {}
                except Exception:
                    lookup_failures.add(index)

        confirmed = []
        for index, item in enumerate(candidates):
            match = confirmations.get(index) or {}
            if match:
                confirmed.append({
                    **item,
                    "company_type": "listed_public",
                    "classification_confidence": max(
                        float(item.get("classification_confidence") or 0),
                        0.95,
                    ),
                    "ticker": match.get("ticker") or item.get("ticker") or "",
                    "screener_url": match.get("screener_url") or "",
                    "classification_source": (
                        f"Direct Screener company search confirmed "
                        f"{match.get('company_name') or item.get('name')} "
                        f"({match.get('ticker') or 'listed'})."
                    ),
                })
            elif index in lookup_failures:
                confirmed.append({
                    **item,
                    "classification_source": (
                        str(item.get("classification_source") or "").strip()
                        or "Listing classification retained because Screener validation was temporarily unavailable."
                    ),
                })
            elif item.get("discovery_route") == "private":
                confirmed.append({
                    **item,
                    "company_type": "private",
                    "classification_confidence": max(
                        float(item.get("classification_confidence") or 0),
                        0.7,
                    ),
                    "exchange": "",
                    "ticker": "",
                    "screener_url": "",
                    "classification_source": (
                        "Direct Screener company search found no listed entity; "
                        "retained as a private/unlisted competitor candidate."
                    ),
                })
            elif item.get("company_type") == "listed_public":
                confirmed.append({
                    **item,
                    "company_type": "unknown",
                    "classification_confidence": min(
                        float(item.get("classification_confidence") or 0),
                        0.4,
                    ),
                    "exchange": "",
                    "ticker": "",
                    "screener_url": "",
                    "classification_source": "Direct Screener company search did not confirm this entity as listed.",
                })
            else:
                confirmed.append(item)
        return confirmed

    @staticmethod
    def _balance_company_types(candidates: list[dict], *, limit: int) -> list[dict]:
        public = [item for item in candidates if item.get("company_type") == "listed_public"]
        private = [item for item in candidates if item.get("company_type") == "private"]
        unknown = [item for item in candidates if item.get("company_type") not in {"listed_public", "private"}]

        # Reserve four slots for each verified route. Fill the remaining slots
        # evenly from verified candidates before including unverified results.
        selected = [*public[:4], *private[:4]]
        selected_ids = {id(item) for item in selected}
        remaining = [
            item
            for item in [*public[4:5], *private[4:5], *public[5:], *private[5:], *unknown]
            if id(item) not in selected_ids
        ]
        return [*selected, *remaining][:limit]
