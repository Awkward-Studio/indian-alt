"""One idempotent deal synthesis run across ledger fields and financial evidence."""

from __future__ import annotations

import logging
from copy import deepcopy

from django.db import transaction

from deals.models import Deal, DealAnalysis
from deals.services.deal_field_synthesis import DealFieldSynthesisService
from deals.services.internal_financial_profile import (
    InternalFinancialProfileService,
    NoSupportedFinancialData,
)


logger = logging.getLogger(__name__)


class DealSynthesisService:
    """Run core field synthesis and a non-blocking, provenance-safe financial pass."""

    TERMINAL_FINANCIAL_STATUSES = {"completed", "skipped", "completed_with_warning"}

    @classmethod
    def run(
        cls,
        deal: Deal,
        *,
        batch_key: str,
        source_type: str,
        required_document_ids: list[str] | None = None,
        parent_audit_log_id: str | None = None,
    ) -> dict:
        analysis = DealFieldSynthesisService.synthesize(
            deal,
            batch_key=batch_key,
            source_type=source_type,
            required_document_ids=required_document_ids,
        )
        analysis.refresh_from_db(fields=["analysis_json"])
        analysis_payload = analysis.analysis_json if isinstance(analysis.analysis_json, dict) else {}
        prior_financial = (analysis_payload.get("metadata") or {}).get("financial_synthesis")
        if (
            isinstance(prior_financial, dict)
            and prior_financial.get("status") in cls.TERMINAL_FINANCIAL_STATUSES
        ):
            return {"analysis": analysis, "financial": prior_financial, "idempotent_replay": True}

        try:
            summary = InternalFinancialProfileService().extract(
                deal=deal,
                parent_audit_log_id=parent_audit_log_id,
            )
            financial = {"status": "completed", "summary": summary, "warning": None}
        except NoSupportedFinancialData as exc:
            financial = {"status": "skipped", "summary": {}, "warning": str(exc)}
        except Exception as exc:
            logger.exception("Financial extraction failed after deal synthesis for %s", deal.id)
            financial = {
                "status": "completed_with_warning",
                "summary": {},
                "warning": str(exc),
            }

        with transaction.atomic():
            locked = DealAnalysis.objects.select_for_update().get(pk=analysis.pk)
            payload = deepcopy(locked.analysis_json or {})
            payload.setdefault("metadata", {})["financial_synthesis"] = financial
            locked.analysis_json = payload
            locked.save(update_fields=["analysis_json"])
            analysis = locked
        return {"analysis": analysis, "financial": financial, "idempotent_replay": False}
