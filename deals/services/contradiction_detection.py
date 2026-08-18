from __future__ import annotations

import json
import hashlib
import math
import re
import time
from dataclasses import asdict, dataclass
from itertools import combinations
from typing import Any, Iterable

from django.conf import settings
from django.utils import timezone

from ai_orchestrator.services.llm_providers import VLLMProviderService
from ai_orchestrator.services.pipeline_registry import PipelineRegistryService
from ai_orchestrator.services.runtime import AIRuntimeService
from deals.services.document_artifacts import DocumentArtifactService


@dataclass(frozen=True)
class ClaimEvidence:
    source_type: str
    source_id: str
    source_label: str
    passage: str
    location: str = ""
    url: str = ""
    observed_at: str = ""


@dataclass(frozen=True)
class StructuredClaim:
    subject: str
    metric: str
    value: float
    value_text: str
    unit: str
    period: str
    evidence: ClaimEvidence
    confidence: str = "unknown"
    qualifier: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "value": self.value,
        }


@dataclass(frozen=True)
class ClaimComparison:
    subject: str
    metric: str
    period: str
    unit: str
    left: StructuredClaim
    right: StructuredClaim
    numeric_relation: str
    absolute_delta: float | None
    relative_delta_percent: float | None
    classification_status: str = "requires_classification"

    def as_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "metric": self.metric,
            "period": self.period,
            "unit": self.unit,
            "left": self.left.as_dict(),
            "right": self.right.as_dict(),
            "numeric_relation": self.numeric_relation,
            "absolute_delta": self.absolute_delta,
            "relative_delta_percent": self.relative_delta_percent,
            "classification_status": self.classification_status,
        }


@dataclass(frozen=True)
class DiscrepancyClassification:
    classification: str
    confidence: float
    rationale: str
    materiality: str
    left_evidence: ClaimEvidence
    right_evidence: ClaimEvidence
    model_used: str = ""
    classifier_version: str = "1"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ContradictionDetectionService:
    """
    Normalizes evidence-bearing claims for later discrepancy classification.

    This service deliberately does not decide whether a difference is a
    contradiction. It only creates comparable, cross-source claim pairs. The
    classifier and analyst-review workflow are separate feature stages.
    """

    METRIC_ALIASES = (
        (
            "ebitda_margin",
            re.compile(r"\b(?:adjusted\s+)?ebitda\s+margin\b", re.IGNORECASE),
        ),
        (
            "growth_percent",
            re.compile(
                r"\b(?:revenue|sales|turnover)?\s*(?:yoy\s+|year[- ]on[- ]year\s+)?"
                r"(?:growth|cagr)\b",
                re.IGNORECASE,
            ),
        ),
        (
            "revenue",
            re.compile(r"\b(?:net\s+sales|sales|turnover|revenue)\b", re.IGNORECASE),
        ),
        (
            "ebitda",
            re.compile(r"\b(?:adjusted\s+)?ebitda\b", re.IGNORECASE),
        ),
        (
            "valuation",
            re.compile(
                r"\b(?:pre[- ]money|post[- ]money|enterprise\s+value|market\s+cap(?:italization)?|"
                r"valuation)\b",
                re.IGNORECASE,
            ),
        ),
        (
            "shareholding_percent",
            re.compile(
                r"\b(?:promoter\s+holding|non[- ]promoter\s+holding|shareholding|ownership|stake)\b",
                re.IGNORECASE,
            ),
        ),
    )
    PERIOD_PATTERN = re.compile(
        r"\b(?:(Q[1-4])\s*)?(FY|CY)?\s*(20\d{2}|\d{2})(?:\s*[-/]\s*(\d{2,4}))?\b",
        re.IGNORECASE,
    )
    NUMBER_PATTERN = re.compile(
        r"(?P<currency>₹|INR|Rs\.?|USD|\$)?\s*"
        r"(?P<number>[+-]?\d[\d,]*(?:\.\d+)?)\s*"
        r"(?P<scale>crores?|cr\.?|lakhs?|lacs?|mn|million|bn|billion)?\s*"
        r"(?P<percent>%|percent|percentage\s+points?)?",
        re.IGNORECASE,
    )
    TEXT_CLAIM_PATTERN = re.compile(
        r"(?P<metric>"
        r"(?:adjusted\s+)?EBITDA(?:\s+margin)?|"
        r"(?:revenue|sales|turnover)(?:\s+(?:growth|CAGR))?|"
        r"(?:year[- ]on[- ]year|YoY)\s+growth|"
        r"(?:pre[- ]money|post[- ]money|enterprise\s+value|market\s+cap(?:italization)?|valuation)|"
        r"(?:promoter|non[- ]promoter)?\s*(?:shareholding|ownership|stake)"
        r")"
        r"[^.\n;]{0,80}?"
        r"(?P<value>(?:₹|INR|Rs\.?|USD|\$)?\s*[+-]?\d[\d,]*(?:\.\d+)?\s*"
        r"(?:crores?|cr\.?|lakhs?|lacs?|mn|million|bn|billion|%|percent)?)",
        re.IGNORECASE,
    )

    @classmethod
    def collect_deal_claims(cls, deal) -> list[StructuredClaim]:
        claims: list[StructuredClaim] = []
        for document in deal.documents.all().order_by("created_at", "id"):
            claims.extend(cls.extract_document_claims(document, subject=deal.title or str(deal.id)))
        for note in deal.meeting_notes.all().order_by("meeting_at", "created_at", "id"):
            claims.extend(cls.extract_meeting_claims(note, subject=deal.title or str(deal.id)))
        target_relations = deal.vi_relations.filter(relation_type="target").select_related(
            "company_profile"
        )
        for relation in target_relations:
            claims.extend(
                cls.extract_public_profile_claims(
                    relation.company_profile,
                    subject=deal.title or str(deal.id),
                )
            )
        return cls._deduplicate(claims)

    @classmethod
    def extract_document_claims(
        cls,
        document_or_artifact: Any,
        *,
        subject: str,
    ) -> list[StructuredClaim]:
        artifact = (
            document_or_artifact
            if isinstance(document_or_artifact, dict)
            else DocumentArtifactService.artifact_from_document(document_or_artifact)
        )
        source_id = str(
            artifact.get("source_id")
            or getattr(document_or_artifact, "id", "")
            or artifact.get("document_name")
            or ""
        )
        source_label = str(
            artifact.get("document_name")
            or getattr(document_or_artifact, "title", "")
            or "Deal document"
        )
        source_url = str(getattr(document_or_artifact, "file_url", "") or "")
        claims: list[StructuredClaim] = []
        for item in [*(artifact.get("metrics") or []), *(artifact.get("numeric_evidence") or [])]:
            if not isinstance(item, dict):
                continue
            metric_label = item.get("name") or item.get("line_item") or item.get("metric")
            claim = cls._claim_from_fields(
                subject=subject,
                metric_label=metric_label,
                raw_value=item.get("value"),
                raw_unit=item.get("unit"),
                raw_period=item.get("period"),
                confidence=item.get("confidence"),
                qualifier=item.get("notes"),
                evidence=ClaimEvidence(
                    source_type="deal_document",
                    source_id=source_id,
                    source_label=source_label,
                    passage=cls._passage(item),
                    location=str(item.get("source_location") or ""),
                    url=source_url,
                ),
            )
            if claim:
                claims.append(claim)

        for raw_claim in artifact.get("claims") or []:
            if not isinstance(raw_claim, str):
                continue
            claims.extend(
                cls.extract_text_claims(
                    raw_claim,
                    subject=subject,
                    evidence=ClaimEvidence(
                        source_type="deal_document",
                        source_id=source_id,
                        source_label=source_label,
                        passage=raw_claim.strip()[:1000],
                        location=cls._source_location(artifact),
                        url=source_url,
                    ),
                )
            )
        return cls._deduplicate(claims)

    @classmethod
    def extract_meeting_claims(cls, note: Any, *, subject: str) -> list[StructuredClaim]:
        observed_at = ""
        meeting_at = getattr(note, "meeting_at", None)
        if meeting_at:
            observed_at = meeting_at.isoformat()
        evidence_base = {
            "source_type": "meeting_note",
            "source_id": str(getattr(note, "id", "") or ""),
            "source_label": str(getattr(note, "title", "") or "Meeting note"),
            "observed_at": observed_at,
        }
        claims: list[StructuredClaim] = []
        for text in (getattr(note, "summary", ""), getattr(note, "body", "")):
            for passage in cls._evidence_lines(text):
                claims.extend(
                    cls.extract_text_claims(
                        passage,
                        subject=subject,
                        evidence=ClaimEvidence(passage=passage[:1000], **evidence_base),
                    )
                )
        return cls._deduplicate(claims)

    @classmethod
    def extract_public_profile_claims(
        cls,
        profile: Any,
        *,
        subject: str | None = None,
    ) -> list[StructuredClaim]:
        subject = subject or str(getattr(profile, "name", "") or getattr(profile, "id", ""))
        source_id = str(getattr(profile, "id", "") or "")
        source_label = str(getattr(profile, "name", "") or "Public company profile")
        source_url = str(
            getattr(profile, "screener_url", "")
            or getattr(profile, "website", "")
            or ""
        )
        claims: list[StructuredClaim] = []

        shareholding_fields = (
            ("Promoter shareholding", getattr(profile, "shp_promoter", None)),
            ("Non-promoter shareholding", getattr(profile, "shp_non_promoter", None)),
        )
        for label, raw_value in shareholding_fields:
            claim = cls._claim_from_fields(
                subject=subject,
                metric_label=label,
                raw_value=raw_value,
                raw_unit="%",
                raw_period=getattr(profile, "shp_year", None),
                confidence="high",
                evidence=ClaimEvidence(
                    source_type="public_profile",
                    source_id=source_id,
                    source_label=source_label,
                    passage=f"{label}: {raw_value}",
                    location=label,
                    url=source_url,
                ),
            )
            if claim:
                claims.append(claim)

        statements = getattr(profile, "financial_statements", None)
        if statements is not None:
            for statement in statements.all().order_by("fy", "statement_type", "id"):
                for path, raw_value in cls._flatten_mapping(statement.data or {}):
                    claim = cls._claim_from_fields(
                        subject=subject,
                        metric_label=path.rsplit(".", 1)[-1],
                        raw_value=raw_value,
                        raw_period=statement.fy,
                        confidence="high",
                        evidence=ClaimEvidence(
                            source_type="public_profile",
                            source_id=source_id,
                            source_label=source_label,
                            passage=f"{path}: {raw_value}",
                            location=f"{statement.get_statement_type_display()} / {statement.fy} / {path}",
                            url=source_url,
                        ),
                    )
                    if claim:
                        claims.append(claim)

        for path, raw_value in cls._flatten_mapping(
            getattr(profile, "public_market_snapshot", {}) or {}
        ):
            claim = cls._claim_from_fields(
                subject=subject,
                metric_label=path.rsplit(".", 1)[-1],
                raw_value=raw_value,
                confidence="medium",
                evidence=ClaimEvidence(
                    source_type="public_profile",
                    source_id=source_id,
                    source_label=source_label,
                    passage=f"{path}: {raw_value}",
                    location=f"public_market_snapshot.{path}",
                    url=source_url,
                ),
            )
            if claim:
                claims.append(claim)
        return cls._deduplicate(claims)

    @classmethod
    def extract_text_claims(
        cls,
        text: str,
        *,
        subject: str,
        evidence: ClaimEvidence,
    ) -> list[StructuredClaim]:
        claims: list[StructuredClaim] = []
        for match in cls.TEXT_CLAIM_PATTERN.finditer(text or ""):
            claim = cls._claim_from_fields(
                subject=subject,
                metric_label=match.group("metric"),
                raw_value=match.group("value"),
                raw_period=cls._period_from_text(match.group(0)),
                evidence=evidence,
            )
            if claim:
                claims.append(claim)
        return cls._deduplicate(claims)

    @classmethod
    def group_claims(
        cls,
        claims: Iterable[StructuredClaim],
    ) -> dict[tuple[str, str, str, str], list[StructuredClaim]]:
        grouped: dict[tuple[str, str, str, str], list[StructuredClaim]] = {}
        for claim in claims:
            key = (
                cls._normalize_subject(claim.subject),
                claim.metric,
                claim.period,
                claim.unit,
            )
            grouped.setdefault(key, []).append(claim)
        return grouped

    @classmethod
    def build_comparison_candidates(
        cls,
        claims: Iterable[StructuredClaim],
        *,
        max_candidates: int = 250,
    ) -> list[ClaimComparison]:
        max_candidates = max(1, min(int(max_candidates), 1000))
        comparisons: list[ClaimComparison] = []
        for group in cls.group_claims(claims).values():
            for left, right in combinations(group, 2):
                if (
                    left.evidence.source_type == right.evidence.source_type
                    and left.evidence.source_id == right.evidence.source_id
                ):
                    continue
                if not left.evidence.passage or not right.evidence.passage:
                    continue
                delta = abs(left.value - right.value)
                relation = "equal" if math.isclose(left.value, right.value, rel_tol=1e-9, abs_tol=1e-9) else "different"
                baseline = max(abs(left.value), abs(right.value))
                relative_delta = (delta / baseline * 100) if baseline else (0.0 if not delta else None)
                comparisons.append(
                    ClaimComparison(
                        subject=left.subject,
                        metric=left.metric,
                        period=left.period,
                        unit=left.unit,
                        left=left,
                        right=right,
                        numeric_relation=relation,
                        absolute_delta=round(delta, 6),
                        relative_delta_percent=(
                            round(relative_delta, 4) if relative_delta is not None else None
                        ),
                    )
                )
                if len(comparisons) >= max_candidates:
                    return comparisons
        return comparisons

    @classmethod
    def _claim_from_fields(
        cls,
        *,
        subject: str,
        metric_label: Any,
        raw_value: Any,
        evidence: ClaimEvidence,
        raw_unit: Any = None,
        raw_period: Any = None,
        confidence: Any = None,
        qualifier: Any = None,
    ) -> StructuredClaim | None:
        metric = cls._normalize_metric(metric_label)
        if not metric:
            return None
        parsed = cls._normalize_value(raw_value, raw_unit, metric=metric)
        if not parsed:
            return None
        value, unit, value_text = parsed
        if not evidence.source_id or not evidence.source_label or not evidence.passage:
            return None
        return StructuredClaim(
            subject=str(subject or "").strip(),
            metric=metric,
            value=value,
            value_text=value_text,
            unit=unit,
            period=cls._normalize_period(raw_period),
            evidence=evidence,
            confidence=cls._normalize_confidence(confidence),
            qualifier=str(qualifier or "").strip(),
        )

    @classmethod
    def _normalize_metric(cls, value: Any) -> str | None:
        label = str(value or "").replace("_", " ").strip()
        for metric, pattern in cls.METRIC_ALIASES:
            if pattern.search(label):
                return metric
        return None

    @classmethod
    def _normalize_value(
        cls,
        raw_value: Any,
        raw_unit: Any,
        *,
        metric: str,
    ) -> tuple[float, str, str] | None:
        if isinstance(raw_value, bool) or raw_value is None:
            return None
        value_text = str(raw_value).strip()
        if not value_text:
            return None
        match = cls.NUMBER_PATTERN.search(value_text)
        if not match:
            return None
        try:
            number = float(match.group("number").replace(",", ""))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number):
            return None

        joined_unit = " ".join(
            str(item or "").strip()
            for item in (match.group("currency"), match.group("scale"), match.group("percent"), raw_unit)
            if str(item or "").strip()
        ).casefold()
        currency = "USD" if "$" in joined_unit or "usd" in joined_unit else "INR"
        if "%" in joined_unit or "percent" in joined_unit or metric.endswith("_percent"):
            return number, "percent", value_text
        if any(token in joined_unit for token in ("crore", " cr")):
            return number, f"{currency}_crore", value_text
        if any(token in joined_unit for token in ("lakh", "lac")):
            return number / 100.0, f"{currency}_crore", value_text
        if re.search(r"\b(?:mn|million)\b", joined_unit):
            factor = 0.1 if currency == "INR" else 1.0
            return number * factor, f"{currency}_{'crore' if currency == 'INR' else 'million'}", value_text
        if re.search(r"\b(?:bn|billion)\b", joined_unit):
            factor = 100.0 if currency == "INR" else 1000.0
            return number * factor, f"{currency}_{'crore' if currency == 'INR' else 'million'}", value_text
        if any(token in joined_unit for token in ("₹", "inr", "rs")):
            return number, "INR", value_text
        if any(token in joined_unit for token in ("$", "usd")):
            return number, "USD", value_text
        return number, "number", value_text

    @classmethod
    def _normalize_period(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return "unspecified"
        match = cls.PERIOD_PATTERN.search(text)
        if not match:
            return text.upper()
        quarter, basis, year, end_year = match.groups()
        selected_year = end_year or year
        if len(selected_year) == 2:
            selected_year = f"20{selected_year}"
        prefix = (basis or ("FY" if end_year else "")).upper()
        normalized = f"{prefix}{selected_year}" if prefix else selected_year
        return f"{quarter.upper()} {normalized}" if quarter else normalized

    @classmethod
    def _period_from_text(cls, text: str) -> str:
        match = cls.PERIOD_PATTERN.search(text or "")
        return cls._normalize_period(match.group(0)) if match else "unspecified"

    @staticmethod
    def _normalize_confidence(value: Any) -> str:
        normalized = str(value or "").strip().casefold()
        return normalized if normalized in {"high", "medium", "low"} else "unknown"

    @staticmethod
    def _normalize_subject(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())

    @staticmethod
    def _source_location(artifact: dict[str, Any]) -> str:
        source_map = artifact.get("source_map") if isinstance(artifact.get("source_map"), dict) else {}
        return " / ".join(
            str(value)
            for value in (source_map.get("section"), source_map.get("page"))
            if value not in (None, "")
        )

    @staticmethod
    def _passage(item: dict[str, Any]) -> str:
        return json.dumps(item, ensure_ascii=False, default=str)[:1000]

    @staticmethod
    def _evidence_lines(text: Any) -> list[str]:
        return [
            line.strip()
            for line in re.split(r"[\n;]+", str(text or ""))
            if line.strip()
        ]

    @classmethod
    def _deduplicate(cls, claims: Iterable[StructuredClaim]) -> list[StructuredClaim]:
        seen: set[tuple[Any, ...]] = set()
        result: list[StructuredClaim] = []
        for claim in claims:
            key = (
                cls._normalize_subject(claim.subject),
                claim.metric,
                claim.value,
                claim.unit,
                claim.period,
                claim.evidence.source_type,
                claim.evidence.source_id,
                claim.evidence.location,
            )
            if key not in seen:
                seen.add(key)
                result.append(claim)
        return result

    @staticmethod
    def _flatten_mapping(
        value: Any,
        *,
        prefix: str = "",
        depth: int = 0,
    ) -> Iterable[tuple[str, Any]]:
        if depth > 4:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                path = f"{prefix}.{key}" if prefix else str(key)
                yield from ContradictionDetectionService._flatten_mapping(
                    child,
                    prefix=path,
                    depth=depth + 1,
                )
        elif isinstance(value, list):
            for index, child in enumerate(value[:50]):
                path = f"{prefix}[{index}]"
                yield from ContradictionDetectionService._flatten_mapping(
                    child,
                    prefix=path,
                    depth=depth + 1,
                )
        elif prefix:
            yield prefix, value


class DiscrepancyClassifier:
    """
    Classifies a claim pair without mutating canonical deal data.

    Deterministic gates handle evidence, period, unit, estimate, and opinion
    distinctions before an LLM is consulted. Model output is validated again
    against those gates, so it cannot turn incomparable claims into a supported
    contradiction.
    """

    CLASSIFICATIONS = (
        "contradiction",
        "definition_difference",
        "time_period_difference",
        "estimate",
        "opinion",
        "insufficient_evidence",
        "no_discrepancy",
    )
    MATERIALITY_LEVELS = ("high", "medium", "low", "unknown")
    ESTIMATE_PATTERN = re.compile(
        r"\b(?:estimate[ds]?|forecast(?:s|ed|ing)?|project(?:ed|ion)?|budget(?:ed)?|"
        r"expected|guidance|run[- ]rate|target)\b",
        re.IGNORECASE,
    )
    OPINION_PATTERN = re.compile(
        r"\b(?:believe[ds]?|think|likely|unlikely|appears?|seems?|"
        r"strong|weak|better|worse|promising)\b",
        re.IGNORECASE,
    )

    def __init__(self, *, llm_service=None, model: str | None = None):
        self.llm_service = llm_service or VLLMProviderService()
        self.model = (
            model
            or AIRuntimeService.get_text_model()
            or "local-model"
        )

    @classmethod
    def response_format(cls) -> dict[str, Any]:
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "claim_discrepancy_classification",
                "strict": True,
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "classification": {
                            "type": "string",
                            "enum": list(cls.CLASSIFICATIONS),
                        },
                        "confidence": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                        },
                        "rationale": {"type": "string"},
                        "materiality": {
                            "type": "string",
                            "enum": list(cls.MATERIALITY_LEVELS),
                        },
                    },
                    "required": [
                        "classification",
                        "confidence",
                        "rationale",
                        "materiality",
                    ],
                },
            },
        }

    def classify(
        self,
        left: StructuredClaim,
        right: StructuredClaim,
    ) -> DiscrepancyClassification:
        gated = self._deterministic_gate(left, right)
        if gated:
            return gated

        system_prompt, prompt, _ = PipelineRegistryService.render_prompt_stage(
            "analysis_support",
            "contradiction_classifier",
            claim_pair_json=json.dumps(self._classification_payload(left, right), ensure_ascii=False, default=str),
        )
        try:
            result = self.llm_service.execute_standard(
                {
                    "model": self.model,
                    "messages": [
                        {
                            "role": "system",
                            "content": system_prompt,
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0,
                    "max_tokens": 700,
                    "response_format": self.response_format(),
                    "thinking": False,
                    "enable_thinking": False,
                },
                timeout=120,
            )
            parsed = self._parse_response(result.get("response"))
        except Exception as exc:
            return self._result(
                "insufficient_evidence",
                0.0,
                f"Classifier unavailable or returned invalid output: {exc}",
                "unknown",
                left,
                right,
            )

        classification = parsed["classification"]
        if classification == "contradiction":
            post_gate = self._contradiction_guard(left, right)
            if post_gate:
                return post_gate
        return self._result(
            classification,
            parsed["confidence"],
            parsed["rationale"],
            parsed["materiality"],
            left,
            right,
            model_used=self.model,
        )

    def classify_many(
        self,
        comparisons: Iterable[ClaimComparison],
    ) -> list[DiscrepancyClassification]:
        return [
            self.classify(comparison.left, comparison.right)
            for comparison in comparisons
        ]

    def run_for_deal(
        self,
        deal: Any,
        *,
        requested_by: Any = None,
        max_comparisons: int = 100,
    ) -> dict[str, Any]:
        from ai_orchestrator.models import AIAuditLog

        started_at = time.monotonic()
        max_comparisons = max(1, min(int(max_comparisons), 250))
        audit_log = AIAuditLog.objects.create(
            source_type="deal_contradiction_detection",
            source_id=str(deal.id),
            context_label=f"Contradiction detection: {deal.title}",
            requested_by=requested_by,
            model_provider="vllm",
            model_used=self.model,
            system_prompt=(
                "Compare normalized, evidence-bearing claims. Never mutate canonical "
                "deal data and never classify unsupported differences as contradictions."
            ),
            user_prompt=f"Detect bounded discrepancies for deal {deal.id}.",
            raw_response="",
            parsed_json={},
            status="PROCESSING",
            is_success=False,
            source_metadata={
                "deal_id": str(deal.id),
                "workflow": "deal_contradiction_detection",
                "max_comparisons": max_comparisons,
            },
        )
        try:
            claims = ContradictionDetectionService.collect_deal_claims(deal)
            comparisons = ContradictionDetectionService.build_comparison_candidates(
                claims,
                max_candidates=max_comparisons,
            )
            persisted = []
            classifications: dict[str, int] = {}
            for comparison in comparisons:
                result = self.classify(comparison.left, comparison.right)
                classifications[result.classification] = (
                    classifications.get(result.classification, 0) + 1
                )
                if result.classification == "no_discrepancy":
                    continue
                record, created = self.persist_classification(
                    deal=deal,
                    left=comparison.left,
                    right=comparison.right,
                    classification=result,
                    audit_log=audit_log,
                )
                persisted.append(
                    {
                        "id": str(record.id),
                        "classification": result.classification,
                        "created": created,
                    }
                )
            summary = {
                "deal_id": str(deal.id),
                "claims": len(claims),
                "comparisons": len(comparisons),
                "persisted": len(persisted),
                "classification_counts": classifications,
                "records": persisted,
                "bounded": len(comparisons) >= max_comparisons,
            }
            audit_log.raw_response = json.dumps(summary, sort_keys=True)
            audit_log.parsed_json = summary
            audit_log.request_duration_ms = round(
                (time.monotonic() - started_at) * 1000
            )
            audit_log.status = "COMPLETED"
            audit_log.is_success = True
            audit_log.completed_at = timezone.now()
            audit_log.source_metadata = {
                **(audit_log.source_metadata or {}),
                "claim_count": len(claims),
                "comparison_count": len(comparisons),
                "persisted_count": len(persisted),
            }
            audit_log.save(
                update_fields=[
                    "raw_response",
                    "parsed_json",
                    "request_duration_ms",
                    "status",
                    "is_success",
                    "completed_at",
                    "source_metadata",
                ]
            )
            return {**summary, "audit_log_id": str(audit_log.id)}
        except Exception as exc:
            audit_log.status = "FAILED"
            audit_log.error_message = str(exc)
            audit_log.request_duration_ms = round(
                (time.monotonic() - started_at) * 1000
            )
            audit_log.completed_at = timezone.now()
            audit_log.save(
                update_fields=[
                    "status",
                    "error_message",
                    "request_duration_ms",
                    "completed_at",
                ]
            )
            raise

    @staticmethod
    def persist_classification(
        *,
        deal: Any,
        left: StructuredClaim,
        right: StructuredClaim,
        classification: DiscrepancyClassification,
        audit_log: Any = None,
    ):
        from deals.models import DealContradiction

        identity = {
            "deal_id": str(deal.id),
            "metric": left.metric,
            "periods": sorted([left.period, right.period]),
            "sources": sorted(
                [
                    f"{left.evidence.source_type}:{left.evidence.source_id}",
                    f"{right.evidence.source_type}:{right.evidence.source_id}",
                ]
            ),
            "values": sorted(
                [
                    f"{left.value}:{left.unit}",
                    f"{right.value}:{right.unit}",
                ]
            ),
        }
        fingerprint = hashlib.sha256(
            json.dumps(identity, sort_keys=True).encode("utf-8")
        ).hexdigest()
        defaults = {
            "subject": left.subject,
            "metric": left.metric,
            "period": left.period if left.period == right.period else "",
            "unit": left.unit if left.unit == right.unit else "",
            "classification": classification.classification,
            "confidence": classification.confidence,
            "materiality": classification.materiality,
            "rationale": classification.rationale,
            "left_claim": left.as_dict(),
            "right_claim": right.as_dict(),
            "classifier_version": classification.classifier_version,
            "model_used": classification.model_used,
            "audit_log": audit_log,
        }
        record, created = DealContradiction.objects.get_or_create(
            deal=deal,
            fingerprint=fingerprint,
            defaults=defaults,
        )
        if not created and record.review_status == DealContradiction.ReviewStatus.UNREVIEWED:
            for field, value in defaults.items():
                setattr(record, field, value)
            record.save(update_fields=[*defaults.keys(), "updated_at"])
        return record, created

    def _deterministic_gate(
        self,
        left: StructuredClaim,
        right: StructuredClaim,
    ) -> DiscrepancyClassification | None:
        if not self._has_retrievable_evidence(left) or not self._has_retrievable_evidence(right):
            return self._result(
                "insufficient_evidence",
                1.0,
                "Both sides require a retrievable source identifier and supporting passage.",
                "unknown",
                left,
                right,
            )
        if (
            ContradictionDetectionService._normalize_subject(left.subject)
            != ContradictionDetectionService._normalize_subject(right.subject)
            or left.metric != right.metric
        ):
            return self._result(
                "insufficient_evidence",
                1.0,
                "The claims do not describe the same subject and metric.",
                "unknown",
                left,
                right,
            )
        if (
            left.period != right.period
            and left.period != "unspecified"
            and right.period != "unspecified"
        ):
            return self._result(
                "time_period_difference",
                1.0,
                f"The claims refer to different normalized periods: {left.period} and {right.period}.",
                "unknown",
                left,
                right,
            )
        if left.unit != right.unit:
            return self._result(
                "definition_difference",
                0.98,
                f"The values use incomparable normalized units: {left.unit} and {right.unit}.",
                "unknown",
                left,
                right,
            )

        combined = " ".join(
            (
                left.qualifier,
                left.evidence.passage,
                right.qualifier,
                right.evidence.passage,
            )
        )
        if self.ESTIMATE_PATTERN.search(combined):
            return self._result(
                "estimate",
                0.95,
                "At least one claim is explicitly framed as an estimate, forecast, target, or guidance.",
                "unknown",
                left,
                right,
            )
        if self.OPINION_PATTERN.search(combined):
            return self._result(
                "opinion",
                0.9,
                "At least one claim is expressed as an opinion rather than a settled fact.",
                "unknown",
                left,
                right,
            )
        if math.isclose(left.value, right.value, rel_tol=1e-9, abs_tol=1e-9):
            return self._result(
                "no_discrepancy",
                1.0,
                "The normalized values agree.",
                "low",
                left,
                right,
            )
        return None

    def _contradiction_guard(
        self,
        left: StructuredClaim,
        right: StructuredClaim,
    ) -> DiscrepancyClassification | None:
        if left.period == "unspecified" or right.period == "unspecified":
            return self._result(
                "insufficient_evidence",
                1.0,
                "A contradiction cannot be supported until both claim periods are known.",
                "unknown",
                left,
                right,
            )
        if left.qualifier and right.qualifier and left.qualifier.casefold() != right.qualifier.casefold():
            return self._result(
                "definition_difference",
                0.9,
                "The claims carry different definitions or qualifiers.",
                "unknown",
                left,
                right,
            )
        return None

    def _classification_payload(
        self,
        left: StructuredClaim,
        right: StructuredClaim,
    ) -> dict[str, Any]:
        return {
            "left_claim": left.as_dict(),
            "right_claim": right.as_dict(),
            "normalized_delta": {
                "absolute": abs(left.value - right.value),
                "relative_percent": self._relative_delta(left.value, right.value),
            },
        }
    @classmethod
    def _parse_response(cls, value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            parsed = value
        else:
            raw = str(value or "").strip()
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                match = re.search(r"\{.*\}", raw, re.DOTALL)
                if not match:
                    raise ValueError("No JSON object found in classifier response.")
                parsed = json.loads(match.group(0))
        if not isinstance(parsed, dict):
            raise ValueError("Classifier response must be an object.")
        classification = str(parsed.get("classification") or "").strip()
        materiality = str(parsed.get("materiality") or "").strip()
        rationale = str(parsed.get("rationale") or "").strip()
        if classification not in cls.CLASSIFICATIONS:
            raise ValueError("Classifier returned an unsupported classification.")
        if materiality not in cls.MATERIALITY_LEVELS:
            raise ValueError("Classifier returned an unsupported materiality.")
        try:
            confidence = float(parsed.get("confidence"))
        except (TypeError, ValueError) as exc:
            raise ValueError("Classifier confidence must be numeric.") from exc
        if not 0 <= confidence <= 1:
            raise ValueError("Classifier confidence must be between 0 and 1.")
        if not rationale:
            raise ValueError("Classifier rationale is required.")
        return {
            "classification": classification,
            "confidence": confidence,
            "rationale": rationale,
            "materiality": materiality,
        }

    def _result(
        self,
        classification: str,
        confidence: float,
        rationale: str,
        materiality: str,
        left: StructuredClaim,
        right: StructuredClaim,
        *,
        model_used: str = "",
    ) -> DiscrepancyClassification:
        return DiscrepancyClassification(
            classification=classification,
            confidence=confidence,
            rationale=rationale,
            materiality=materiality,
            left_evidence=left.evidence,
            right_evidence=right.evidence,
            model_used=model_used,
        )

    @staticmethod
    def _has_retrievable_evidence(claim: StructuredClaim) -> bool:
        return bool(
            claim.evidence.source_type
            and claim.evidence.source_id
            and claim.evidence.source_label
            and claim.evidence.passage
        )

    @staticmethod
    def _relative_delta(left: float, right: float) -> float | None:
        baseline = max(abs(left), abs(right))
        if not baseline:
            return 0.0
        return round(abs(left - right) / baseline * 100, 4)
