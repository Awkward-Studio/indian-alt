from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass
from itertools import combinations
from typing import Any, Iterable

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
    ) -> list[ClaimComparison]:
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
