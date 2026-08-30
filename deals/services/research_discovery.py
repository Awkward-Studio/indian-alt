from __future__ import annotations

import ipaddress
import hashlib
import json
import re
import socket
from datetime import date, datetime
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ai_orchestrator.services.search_provider import SearXNGProviderService
from deals.models import (
    Deal,
    SectorResearchDiscoveryRun,
    SectorResearchRecommendation,
    SectorResearchSourceRule,
)


DEFAULT_PREFERRED_PUBLISHERS = {
    "avendus.com",
    "bain.com",
    "bcg.com",
    "crisil.com",
    "ibef.org",
    "icra.in",
    "mckinsey.com",
    "nseindia.com",
    "sebi.gov.in",
}
BLOCKED_DOMAINS = {
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "twitter.com",
    "x.com",
}


class ResearchDiscoveryCoordinator:
    ACTIVE_STATUSES = {
        SectorResearchDiscoveryRun.Status.QUEUED,
        SectorResearchDiscoveryRun.Status.RUNNING,
    }

    @staticmethod
    def context_hash(deal) -> str:
        target = deal.vi_relations.filter(relation_type="target").select_related(
            "company_profile"
        ).first()
        payload = {
            "title": str(deal.title or "").strip().casefold(),
            "sector": str(deal.sector or "").strip().casefold(),
            "industry": str(deal.industry or "").strip().casefold(),
            "cin": str(
                target.company_profile.cin if target else ""
            ).strip().casefold(),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()

    @classmethod
    def enqueue(cls, *, deal, trigger: str, requested_by=None):
        context_hash = cls.context_hash(deal)
        with transaction.atomic():
            locked_deal = Deal.objects.select_for_update().get(pk=deal.pk)
            existing = (
                SectorResearchDiscoveryRun.objects.filter(
                    deal=locked_deal,
                    context_hash=context_hash,
                    status__in=cls.ACTIVE_STATUSES,
                )
                .order_by("-created_at")
                .first()
            )
            if existing:
                return existing, False

            from ai_orchestrator.services.runtime import AIRuntimeService

            audit_log = AIRuntimeService.create_audit_log(
                source_type="sector_research_discovery",
                source_id=str(locked_deal.id),
                context_label=f"Research discovery — {locked_deal.title or locked_deal.id}",
                status="PENDING",
                is_success=False,
                requested_by=requested_by,
                source_metadata={
                    "deal_id": str(locked_deal.id),
                    "trigger": trigger,
                    "context_hash": context_hash,
                },
            )
            run = SectorResearchDiscoveryRun.objects.create(
                deal=locked_deal,
                trigger=trigger,
                context_hash=context_hash,
                requested_by=requested_by,
                audit_log_id=audit_log.id,
            )

        from deals.tasks import discover_sector_reports_task

        try:
            async_result = discover_sector_reports_task.delay(
                str(deal.id),
                str(run.id),
            )
            run.celery_task_id = str(async_result.id or "")
            run.save(update_fields=["celery_task_id", "updated_at"])
            audit_log.celery_task_id = run.celery_task_id
            audit_log.save(update_fields=["celery_task_id"])
        except Exception as exc:
            run.status = SectorResearchDiscoveryRun.Status.FAILED
            run.error = f"Unable to queue research discovery: {exc}"
            run.completed_at = timezone.now()
            run.save(
                update_fields=["status", "error", "completed_at", "updated_at"]
            )
            audit_log.status = "FAILED"
            audit_log.is_success = False
            audit_log.error_message = run.error
            audit_log.save(
                update_fields=["status", "is_success", "error_message"]
            )
        return run, True


class ResearchDiscoveryService:
    """Discover public research recommendations without bypassing source access."""

    def __init__(self, *, search_service=None, http_session=None):
        self.search_service = search_service or SearXNGProviderService()
        self.http = http_session or requests.Session()
        self.timeout = float(
            getattr(settings, "RESEARCH_DISCOVERY_ACCESS_TIMEOUT", 6) or 6
        )
        try:
            self.source_rules = list(SectorResearchSourceRule.objects.filter(is_active=True))
        except Exception:
            # Discovery remains usable during migrations and isolated unit tests.
            self.source_rules = []
        configured = getattr(settings, "RESEARCH_DISCOVERY_PREFERRED_DOMAINS", [])
        fallback = configured or DEFAULT_PREFERRED_PUBLISHERS
        self.preferred_publishers = {
            rule.domain for rule in self.source_rules if rule.is_preferred
        } or {str(value).strip().lower() for value in fallback if str(value).strip()}

    def discover(self, *, deal, cin: str = "") -> dict[str, Any]:
        title = str(getattr(deal, "title", "") or "").strip()
        sector = str(getattr(deal, "sector", "") or "").strip()
        industry = str(getattr(deal, "industry", "") or "").strip()
        if not any((title, sector, industry, cin)):
            raise ValueError(
                "A company name, sector, industry, or CIN is required for research discovery."
            )

        queries = self.build_queries(
            company_name=title,
            sector=sector,
            industry=industry,
            cin=cin,
        )
        template_context = {
            "company": title, "sector": sector, "industry": industry,
            "cin": cin, "market": " ".join(value for value in (sector, industry) if value).strip(),
        }
        for rule in self.source_rules:
            for template in rule.query_templates:
                try:
                    query = str(template).format(**template_context).strip()
                except (KeyError, ValueError):
                    continue
                if query:
                    queries.append(f"site:{rule.domain} {query}")
        queries = list(dict.fromkeys(queries))
        results = self.search_service.search_many(
            queries,
            results_per_query=8,
            max_results=40,
        )
        recommendations = []
        seen_urls = set()
        for result in results:
            normalized = self.search_service.normalize_url(result.get("url"))
            if not normalized or normalized in seen_urls:
                continue
            seen_urls.add(normalized)
            recommendation = self._normalize_result(
                result=result,
                company_name=title,
                sector=sector,
                industry=industry,
                cin=cin,
            )
            if recommendation:
                recommendation.update(
                    self.score_recommendation(
                        recommendation,
                        company_name=title,
                        sector=sector,
                        industry=industry,
                        cin=cin,
                    )
                )
                recommendations.append(recommendation)

        recommendations.sort(
            key=lambda item: (
                item["total_score"],
                item.get("publication_date") or "",
                item["title"].casefold(),
            ),
            reverse=True,
        )
        return {
            "status": "COMPLETED",
            "queries": queries,
            "retrieved_at": timezone.now().isoformat(),
            "recommendations": recommendations,
            "result_count": len(recommendations),
        }

    @staticmethod
    def _context_tokens(*values: str) -> set[str]:
        ignored = {
            "and", "company", "india", "indian", "limited", "ltd", "private",
            "pvt", "the",
        }
        return {
            token
            for value in values
            for token in re.findall(r"[a-z0-9]{3,}", str(value or "").casefold())
            if token not in ignored
        }

    def score_recommendation(
        self,
        recommendation: dict[str, Any],
        *,
        company_name: str,
        sector: str,
        industry: str,
        cin: str,
    ) -> dict[str, Any]:
        context_tokens = self._context_tokens(company_name, sector, industry, cin)
        result_tokens = self._context_tokens(
            recommendation.get("title", ""),
            recommendation.get("snippet", ""),
            recommendation.get("source_query", ""),
        )
        overlap = (
            len(context_tokens.intersection(result_tokens)) / len(context_tokens)
            if context_tokens
            else 0
        )
        provider_score = min(max(self._safe_float(recommendation.get("search_score")), 0), 5) / 5
        relevance = min(1, (overlap * 0.8) + (provider_score * 0.2))

        domain = str(recommendation.get("publisher_domain") or "")
        credible_suffix = domain.endswith((".gov.in", ".nic.in", ".org", ".edu"))
        credibility = (
            1.0
            if recommendation.get("preferred_source")
            else 0.8 if credible_suffix else 0.5
        )

        publication_date = self._coerce_date(recommendation.get("publication_date"))
        if not publication_date:
            freshness = 0.35
        else:
            age_days = max((timezone.localdate() - publication_date).days, 0)
            freshness = max(0.1, 1 - (age_days / (365 * 5)))

        accessibility = str(recommendation.get("accessibility") or "UNVERIFIED")
        accessibility_score = {
            "AVAILABLE": 1.0,
            "RESTRICTED": 0.25,
            "UNAVAILABLE": 0.0,
            "UNVERIFIED": 0.4,
        }.get(accessibility, 0.4)
        total = (
            relevance * 0.45
            + credibility * 0.25
            + freshness * 0.15
            + accessibility_score * 0.15
        )
        components = {
            "relevance": round(relevance, 4),
            "credibility": round(credibility, 4),
            "freshness": round(freshness, 4),
            "accessibility": round(accessibility_score, 4),
            "weights": {
                "relevance": 0.45,
                "credibility": 0.25,
                "freshness": 0.15,
                "accessibility": 0.15,
            },
            "matched_context_tokens": sorted(
                context_tokens.intersection(result_tokens)
            ),
        }
        return {
            "relevance_score": round(relevance, 4),
            "credibility_score": round(credibility, 4),
            "freshness_score": round(freshness, 4),
            "accessibility_score": round(accessibility_score, 4),
            "total_score": round(total, 4),
            "score_explanation": components,
        }

    @transaction.atomic
    def persist_recommendations(self, *, deal, run, payload: dict) -> list:
        persisted = []
        verified_at = timezone.now()
        for recommendation in payload.get("recommendations", []):
            canonical_url = str(recommendation.get("canonical_url") or "").strip()
            if not canonical_url:
                continue
            publication_date = self._coerce_date(
                recommendation.get("publication_date")
            )
            retrieved_at = self._coerce_datetime(
                recommendation.get("retrieved_at")
            ) or verified_at
            defaults = {
                "run": run,
                "url": recommendation["url"],
                "title": recommendation["title"],
                "publisher": recommendation.get("publisher", ""),
                "publisher_domain": recommendation.get("publisher_domain", ""),
                "publication_date": publication_date,
                "document_type": recommendation.get(
                    "document_type", "OTHER_RESEARCH"
                ),
                "reason": recommendation.get("reason", ""),
                "snippet": recommendation.get("snippet", ""),
                "source_query": recommendation.get("source_query", ""),
                "accessibility": recommendation.get(
                    "accessibility", "UNVERIFIED"
                ),
                "content_type": recommendation.get("content_type", ""),
                "preferred_source": bool(
                    recommendation.get("preferred_source")
                ),
                "relevance_score": recommendation.get("relevance_score", 0),
                "credibility_score": recommendation.get("credibility_score", 0),
                "freshness_score": recommendation.get("freshness_score", 0),
                "accessibility_score": recommendation.get(
                    "accessibility_score", 0
                ),
                "total_score": recommendation.get("total_score", 0),
                "score_explanation": recommendation.get(
                    "score_explanation", {}
                ),
                "retrieved_at": retrieved_at,
                "last_verified_at": verified_at,
            }
            item, _ = SectorResearchRecommendation.objects.update_or_create(
                deal=deal,
                canonical_url=canonical_url,
                defaults=defaults,
            )
            persisted.append(item)
        return persisted

    @staticmethod
    def _coerce_date(value: Any) -> date | None:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value or "")[:10])
        except ValueError:
            return None

    @staticmethod
    def _coerce_datetime(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            return value
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else timezone.make_aware(parsed)
        except ValueError:
            return None

    @staticmethod
    def build_queries(
        *,
        company_name: str,
        sector: str,
        industry: str,
        cin: str,
    ) -> list[str]:
        market = " ".join(value for value in (sector, industry) if value).strip()
        company = company_name or cin
        queries = [
            f'"{market}" India industry report market study filetype:pdf'
            if market
            else f'"{company}" India industry report filetype:pdf',
            f'"{company}" annual report filing filetype:pdf',
            f'"{company}" brokerage research equity report',
        ]
        if cin:
            queries.append(f'"{cin}" MCA ROC filing')
        return list(dict.fromkeys(query for query in queries if query.strip()))

    def _normalize_result(
        self,
        *,
        result: dict,
        company_name: str,
        sector: str,
        industry: str,
        cin: str,
    ) -> dict[str, Any] | None:
        url = str(result.get("url") or "").strip()
        if not self.is_permitted_public_url(url):
            return None
        title = str(result.get("title") or "").strip()
        snippet = str(result.get("snippet") or "").strip()
        if not title and not snippet:
            return None
        domain = (urlparse(url).hostname or "").lower()
        accessibility, content_type = self._probe_access(url)
        document_type = self.classify_document_type(
            title=title,
            snippet=snippet,
            url=url,
        )
        matched_context = [
            value
            for value in (company_name, sector, industry, cin)
            if value and value.casefold() in f"{title} {snippet}".casefold()
        ]
        return {
            "title": title[:500] or domain,
            "publisher": self.publisher_name(domain),
            "publisher_domain": domain,
            "publication_date": self.parse_publication_date(
                result.get("published_date")
            ),
            "url": url,
            "canonical_url": self.search_service.normalize_url(url),
            "document_type": document_type,
            "reason": (
                f"Matches deal context: {', '.join(matched_context[:3])}."
                if matched_context
                else f"Discovered by the {document_type.replace('_', ' ')} search route."
            ),
            "source_query": str(result.get("query") or "").strip(),
            "snippet": snippet[:1500],
            "accessibility": accessibility,
            "content_type": content_type,
            "preferred_source": self._is_preferred_domain(domain),
            "search_score": self._safe_float(result.get("score")),
            "retrieved_at": timezone.now().isoformat(),
        }

    def _probe_access(self, url: str) -> tuple[str, str]:
        current_url = url
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; IndiaAlternativesResearch/1.0)",
            "Range": "bytes=0-2047",
            "Accept": "text/html,application/pdf,*/*;q=0.5",
        }
        try:
            for _ in range(4):
                if not self.is_safe_public_url(current_url):
                    return "UNVERIFIED", ""
                response = self.http.get(
                    current_url,
                    headers=headers,
                    timeout=self.timeout,
                    allow_redirects=False,
                    stream=True,
                )
                if response.status_code in {301, 302, 303, 307, 308}:
                    redirect = response.headers.get("Location")
                    if not redirect:
                        return "UNVERIFIED", ""
                    current_url = urljoin(current_url, redirect)
                    continue
                content_type = str(
                    response.headers.get("Content-Type") or ""
                ).split(";", 1)[0].strip().lower()
                if response.status_code in {200, 206}:
                    return "AVAILABLE", content_type
                if response.status_code in {401, 402, 403}:
                    return "RESTRICTED", content_type
                if response.status_code in {404, 410}:
                    return "UNAVAILABLE", content_type
                return "UNVERIFIED", content_type
        except requests.RequestException:
            return "UNVERIFIED", ""
        return "UNVERIFIED", ""

    @staticmethod
    def classify_document_type(*, title: str, snippet: str, url: str) -> str:
        text = f"{title} {snippet} {url}".casefold()
        if any(marker in text for marker in ("annual report", "10-k", "20-f")):
            return "ANNUAL_REPORT"
        if any(marker in text for marker in ("mca", "roc filing", "statutory filing")):
            return "STATUTORY_FILING"
        if any(marker in text for marker in ("equity research", "brokerage", "analyst report")):
            return "BROKERAGE_RESEARCH"
        if any(marker in text for marker in ("industry report", "market study", "sector report")):
            return "INDUSTRY_REPORT"
        return "OTHER_RESEARCH"

    @staticmethod
    def parse_publication_date(value: Any) -> str | None:
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        raw = str(value or "").strip()
        if not raw:
            return None
        iso_match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", raw)
        if iso_match:
            try:
                return date.fromisoformat(iso_match.group(1)).isoformat()
            except ValueError:
                return None
        year_match = re.search(r"\b(20\d{2})\b", raw)
        return f"{year_match.group(1)}-01-01" if year_match else None

    @staticmethod
    def publisher_name(domain: str) -> str:
        host = domain.removeprefix("www.")
        label = host.split(".", 1)[0].replace("-", " ").strip()
        return label.title() if label else "Unknown publisher"

    def _is_preferred_domain(self, domain: str) -> bool:
        return any(
            domain == preferred or domain.endswith(f".{preferred}")
            for preferred in self.preferred_publishers
        )

    @staticmethod
    def is_permitted_public_url(url: str) -> bool:
        parsed = urlparse(str(url or "").strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        host = parsed.hostname.lower()
        return not any(
            host == blocked or host.endswith(f".{blocked}")
            for blocked in BLOCKED_DOMAINS
        )

    @classmethod
    def is_safe_public_url(cls, url: str) -> bool:
        if not cls.is_permitted_public_url(url):
            return False
        parsed = urlparse(url)
        try:
            addresses = {
                item[4][0]
                for item in socket.getaddrinfo(
                    parsed.hostname,
                    parsed.port or (443 if parsed.scheme == "https" else 80),
                )
            }
            return bool(addresses) and all(
                ipaddress.ip_address(address).is_global for address in addresses
            )
        except (OSError, ValueError):
            return False

    @staticmethod
    def _safe_float(value: Any) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0
