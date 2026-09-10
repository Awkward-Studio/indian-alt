from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from django.db import transaction

from ai_orchestrator.services.ai_processor import AIProcessorService
from ai_orchestrator.services.report_section_evidence import ICReportSectionEvidenceService
from deals.models import (
    DealDocument,
    VentureIntelligenceCompanyProfile,
    VentureIntelligenceCompanyRelation,
    VentureIntelligenceFinancialStatement,
)


PROFILE_FIELDS = {
    "cin",
    "registered_name",
    "website",
    "industry",
    "sector",
    "email",
    "year_founded",
    "city",
    "total_funding",
    "state",
    "region",
    "country",
    "pincode",
    "telephone",
    "phone",
    "linkedin",
    "tags",
    "listing_status",
    "short_name",
    "previous_name",
    "full_name",
    "business_description",
    "incorp_year",
    "company_status",
    "address",
    "address_line2",
    "auditor_name",
    "shp_year",
    "shp_promoter",
    "shp_non_promoter",
    "is_xbrl",
}
INTEGER_FIELDS = {"incorp_year", "shp_year"}
FLOAT_FIELDS = {"shp_promoter", "shp_non_promoter"}
BOOLEAN_FIELDS = {"is_xbrl"}
STATEMENT_TYPES = {"profit_loss", "balance_sheet", "cash_flow"}
METRIC_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,79}$")
FINANCIAL_FIELDS_BY_STATEMENT = {
    "profit_loss": {
        "revenue", "expenses", "gross_profit", "gross_margin", "ebitda",
        "ebitda_margin", "other_income", "interest", "depreciation",
        "profit_before_tax", "tax", "tax_rate", "pat", "pat_margin", "eps",
        "dividend_payout", "employee_cost", "material_cost", "cogs",
        "contribution_margin", "roce", "roe", "roic",
    },
    "balance_sheet": {
        "equity_capital", "reserves", "net_worth", "borrowings", "total_debt",
        "net_debt", "other_liabilities", "total_liabilities", "fixed_assets",
        "cwip", "investments", "inventory", "receivables", "payables",
        "working_capital", "other_assets", "total_assets", "cash_and_equivalents",
        "debtor_days", "inventory_days", "days_payable", "cash_conversion_cycle",
        "working_capital_days",
    },
    "cash_flow": {
        "cash_from_operations", "cash_from_investing", "cash_from_financing",
        "net_cash_flow", "free_cash_flow", "capex",
    },
}


class InternalFinancialProfileService:
    """Fill missing target-company fields using indexed internal evidence only."""

    def __init__(self, *, ai_processor=None, evidence_service_class=None):
        self.ai_processor = ai_processor or AIProcessorService()
        self.evidence_service_class = evidence_service_class or ICReportSectionEvidenceService

    @staticmethod
    def _is_known(value: Any) -> bool:
        return value is not None and value != "" and value != [] and value != {}

    @staticmethod
    def _valid_refs(value: Any, citations: dict[str, dict]) -> list[str]:
        if not isinstance(value, list):
            return []
        return list(dict.fromkeys(str(ref) for ref in value if str(ref) in citations))

    @staticmethod
    def _evidence_by_ref(context: str) -> dict[str, str]:
        matches = list(re.finditer(r"\bR\d{3}\b", context or ""))
        return {
            match.group(0): (context or "")[match.start():matches[index + 1].start() if index + 1 < len(matches) else None]
            for index, match in enumerate(matches)
        }

    @staticmethod
    def _value_supported(value: Any, evidence: str) -> bool:
        rendered = str(value or "").strip().casefold()
        haystack = str(evidence or "").casefold()
        if not rendered or not haystack:
            return False
        normalize = lambda text: re.sub(r"[^a-z0-9.%-]+", " ", text).strip()
        if normalize(rendered) in normalize(haystack):
            return True
        value_numbers = re.findall(r"-?\d+(?:\.\d+)?", rendered.replace(",", ""))
        evidence_numbers = set(re.findall(r"-?\d+(?:\.\d+)?", haystack.replace(",", "")))
        return bool(value_numbers) and all(number in evidence_numbers for number in value_numbers)

    @classmethod
    def _supported_refs(
        cls,
        refs: Any,
        value: Any,
        citations: dict[str, dict],
        evidence_by_ref: dict[str, str],
    ) -> list[str]:
        return [
            ref for ref in cls._valid_refs(refs, citations)
            if cls._value_supported(value, evidence_by_ref.get(ref, ""))
        ]

    @classmethod
    def _coerce_profile_value(cls, field: str, value: Any) -> Any:
        if not cls._is_known(value):
            return None
        try:
            if field in INTEGER_FIELDS:
                return int(value)
            if field in FLOAT_FIELDS:
                return float(value)
            if field in BOOLEAN_FIELDS:
                if isinstance(value, bool):
                    return value
                normalized = str(value).strip().lower()
                if normalized in {"true", "yes", "1"}:
                    return True
                if normalized in {"false", "no", "0"}:
                    return False
                return None
            rendered = str(value).strip()
            max_lengths = {"cin": 21, "year_founded": 10, "pincode": 20}
            if len(rendered) > max_lengths.get(field, 10_000):
                return None
            return rendered
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _prompt(deal_title: str, evidence: str) -> str:
        return f"""Extract only explicitly supported target-company facts from the indexed internal-document evidence below for {deal_title}.

Hard rules:
- Do not use general knowledge, the web, Venture Intelligence, Screener, or inference from silence.
- Every populated value must be copied from and include one or more evidence_refs matching the supplied R### retrieval blocks. The server verifies the value against those referenced blocks.
- Omit uncertain, estimated, derived, conflicting, or absent values. Never use placeholders such as NA or unknown, and never substitute zero for a missing value.
- Preserve reported fiscal period, currency, unit, and standalone/consolidated basis.
- Semantically map source labels to the exact canonical keys listed below. For example, Net Sales / Operating Revenue -> revenue; Operating Profit -> ebitda; Net Profit / Profit After Tax -> pat; CFO -> cash_from_operations.
- A metric value should include its reported unit/currency when the evidence supplies one. Do not convert values.
- Return one JSON object only.
- Prioritize financial information. Extract every explicitly reported historical or projected P&L, balance-sheet, and cash-flow metric across all available periods. Profile fields are secondary.

Schema:
{{
  "profile": {{
    "field_name": {{"value": "supported value", "evidence_refs": ["R001"]}}
  }},
  "financial_statements": [
    {{
      "statement_type": "profit_loss|balance_sheet|cash_flow",
      "fy": "reported fiscal period",
      "fin_type": "Standalone|Consolidated",
      "metrics": {{
        "canonical_metric_key": {{"value": "reported value", "evidence_refs": ["R001"]}}
      }}
    }}
  ]
}}

Allowed profile field names:
{', '.join(sorted(PROFILE_FIELDS))}

Allowed financial fields by statement:
{', '.join(f'{statement}: {"|".join(sorted(fields))}' for statement, fields in FINANCIAL_FIELDS_BY_STATEMENT.items())}

INDEXED INTERNAL EVIDENCE:
{evidence}"""

    def extract(self, *, deal, parent_audit_log_id: str | None = None) -> dict:
        documents = list(
            DealDocument.objects.filter(deal=deal, is_indexed=True).order_by("created_at")
        )
        if not documents:
            raise ValueError("No indexed internal documents are available for this deal.")

        evidence_pack = self.evidence_service_class(
            deal=deal,
            documents=documents,
            candidate_limit=320,
            max_chunks=240,
            max_tokens=36_000,
            min_chunks_per_document=4,
        ).retrieve("Key Financials")
        citations = {
            f"R{int(str(rank).lstrip('Rr')):03d}": citation
            for rank, citation in evidence_pack["citations"].items()
        }
        evidence_by_ref = self._evidence_by_ref(evidence_pack["context"])
        result = self.ai_processor.process_content(
            content=self._prompt(deal.title, evidence_pack["context"]),
            skill_name=None,
            source_type="internal_financial_profile",
            source_id=str(deal.id),
            metadata={
                "response_mode": "json",
                "temperature": 0.0,
                "max_input_tokens": 48_000,
                "max_tokens": 12_000,
                "enforce_context_budget": True,
                "serialize_inference": True,
                "inference_queue_max_wait": 3600,
                "web_search_enabled": False,
                "personality_only_system": True,
                "vdr_parent_audit_id": str(parent_audit_log_id or ""),
                "deal_id": str(deal.id),
                "result_route": f"/deals/{deal.id}?tab=key-financials#venture-intelligence-section",
                "retrieval": evidence_pack["metadata"],
            },
            stream=False,
        )
        if not isinstance(result, dict) or result.get("error"):
            raise ValueError((result or {}).get("error") or "AI returned an invalid extraction.")
        return self.persist(
            deal=deal,
            payload=result,
            citations=citations,
            evidence_by_ref=evidence_by_ref,
        )

    @transaction.atomic
    def persist(
        self,
        *,
        deal,
        payload: dict,
        citations: dict[str, dict],
        evidence_by_ref: dict[str, str] | None = None,
    ) -> dict:
        evidence_by_ref = evidence_by_ref or {}
        relation = (
            VentureIntelligenceCompanyRelation.objects.select_for_update()
            .select_related("company_profile")
            .filter(deal=deal, relation_type="target")
            .first()
        )
        profile = relation.company_profile if relation else None
        profile_payload = payload.get("profile") if isinstance(payload.get("profile"), dict) else {}

        supported_profile: dict[str, tuple[Any, list[str]]] = {}
        for field, item in profile_payload.items():
            if field not in PROFILE_FIELDS or not isinstance(item, dict):
                continue
            value = self._coerce_profile_value(field, item.get("value"))
            refs = self._supported_refs(
                item.get("evidence_refs"), value, citations, evidence_by_ref,
            )
            if refs and self._is_known(value):
                supported_profile[field] = (value, refs)

        rows = payload.get("financial_statements")
        has_supported_financial = False
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or not isinstance(row.get("metrics"), dict):
                continue
            statement_type = str(row.get("statement_type") or "").strip()
            if statement_type not in STATEMENT_TYPES:
                continue
            for key, item in row["metrics"].items():
                clean_key = str(key or "").strip().lower()
                if (
                    clean_key in FINANCIAL_FIELDS_BY_STATEMENT[statement_type]
                    and isinstance(item, dict)
                    and self._is_known(item.get("value"))
                    and isinstance(item.get("value"), (str, int, float))
                    and not isinstance(item.get("value"), bool)
                    and self._supported_refs(
                        item.get("evidence_refs"),
                        item.get("value"),
                        citations,
                        evidence_by_ref,
                    )
                ):
                    has_supported_financial = True
                    break
            if has_supported_financial:
                break
        if profile is None and not supported_profile and not has_supported_financial:
            raise ValueError("The indexed documents did not contain supported company or financial fields.")

        if profile is None:
            cin = supported_profile.get("cin", (None, []))[0]
            profile = VentureIntelligenceCompanyProfile.objects.filter(cin=cin).first() if cin else None
            if profile is None:
                profile = VentureIntelligenceCompanyProfile.objects.create(
                    name=deal.title,
                    data_source="internal_documents",
                    company_type="private",
                )
            relation = VentureIntelligenceCompanyRelation.objects.create(
                deal=deal,
                company_profile=profile,
                relation_type="target",
                notes="Target profile populated from indexed internal documents.",
            )

        profile_updates: list[str] = []
        provenance = deepcopy(profile.raw_profile_json or {})
        internal_provenance = provenance.setdefault("internal_document_extraction", {})
        profile_provenance = internal_provenance.setdefault("profile", {})
        for field, (value, refs) in supported_profile.items():
            if field == "cin" and VentureIntelligenceCompanyProfile.objects.exclude(pk=profile.pk).filter(cin=value).exists():
                continue
            if not self._is_known(getattr(profile, field, None)):
                setattr(profile, field, value)
                profile_updates.append(field)
                profile_provenance[field] = {
                    "evidence_refs": refs,
                    "sources": [citations[ref] for ref in refs],
                }

        statement_count = 0
        metric_count = 0
        statement_provenance = internal_provenance.setdefault("financial_statements", {})
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            statement_type = str(row.get("statement_type") or "").strip()
            fy = str(row.get("fy") or "").strip()
            fin_type = str(row.get("fin_type") or "Standalone").strip().title()
            metrics = row.get("metrics")
            if statement_type not in STATEMENT_TYPES or not fy or len(fy) > 20 or fin_type not in {"Standalone", "Consolidated"} or not isinstance(metrics, dict):
                continue

            supported_metrics = {}
            metric_sources = {}
            for key, item in metrics.items():
                clean_key = str(key or "").strip().lower()
                if (
                    not METRIC_KEY_PATTERN.match(clean_key)
                    or clean_key not in FINANCIAL_FIELDS_BY_STATEMENT[statement_type]
                    or not isinstance(item, dict)
                ):
                    continue
                value = item.get("value")
                refs = self._supported_refs(
                    item.get("evidence_refs"), value, citations, evidence_by_ref,
                )
                if refs and self._is_known(value) and isinstance(value, (str, int, float)) and not isinstance(value, bool):
                    supported_metrics[clean_key] = value
                    metric_sources[clean_key] = {
                        "evidence_refs": refs,
                        "sources": [citations[ref] for ref in refs],
                    }
            if not supported_metrics:
                continue

            statement, created = VentureIntelligenceFinancialStatement.objects.select_for_update().get_or_create(
                company_profile=profile,
                statement_type=statement_type,
                fy=fy,
                fin_type=fin_type,
                defaults={
                    "data": {"fy": fy, "fin_type": fin_type},
                    "data_source": "local_ai",
                    "provenance": {"metrics": {}},
                },
            )
            data = dict(statement.data or {})
            added = []
            for key, value in supported_metrics.items():
                if not self._is_known(data.get(key)):
                    data[key] = value
                    added.append(key)
            if added:
                statement.data = data
                statement.data_source = "local_ai" if created or statement.data_source == "local_ai" else "mixed"
                row_provenance = deepcopy(statement.provenance or {})
                row_provenance.setdefault("metrics", {}).update({
                    key: {**metric_sources[key], "source": "local_ai"}
                    for key in added
                })
                statement.provenance = row_provenance
                statement.save(update_fields=["data", "data_source", "provenance"])
                statement_count += 1
                metric_count += len(added)
                statement_key = f"{statement_type}:{fy}:{fin_type}"
                statement_provenance.setdefault(statement_key, {}).update(
                    {key: metric_sources[key] for key in added}
                )
            elif created:
                statement.delete()

        if profile_updates or metric_count:
            profile.raw_profile_json = provenance
            update_fields = [*profile_updates, "raw_profile_json", "updated_at"]
            profile.save(update_fields=list(dict.fromkeys(update_fields)))

        return {
            "profile_id": str(profile.id),
            "relation_id": str(relation.id),
            "profile_fields_added": len(profile_updates),
            "financial_rows_updated": statement_count,
            "financial_metrics_added": metric_count,
            "indexed_documents_used": len({source["document_id"] for source in citations.values()}),
        }
