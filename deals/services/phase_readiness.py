import json
from typing import Any

from ai_orchestrator.models import AISkill, AIAuditLog

from deals.models import Deal, DealPhase


PHASE_READINESS_SKILL_NAME = "deal_phase_readiness"
PHASE_READINESS_SOURCE_TYPE = "deal_phase_readiness"

ORDERED_DEAL_PHASES = [
    DealPhase.STAGE_1,
    DealPhase.STAGE_2,
    DealPhase.STAGE_3,
    DealPhase.STAGE_4,
    DealPhase.STAGE_5,
    DealPhase.STAGE_6,
    DealPhase.STAGE_7,
    DealPhase.STAGE_8,
    DealPhase.STAGE_9,
    DealPhase.STAGE_10,
    DealPhase.STAGE_11,
    DealPhase.STAGE_12,
    DealPhase.STAGE_13,
    DealPhase.STAGE_14,
    DealPhase.STAGE_15,
    DealPhase.STAGE_16,
    DealPhase.STAGE_17,
    DealPhase.STAGE_18,
]

PHASE_READINESS_PROMPT = """Evaluate whether this deal is ready to move to its next phase.

Input context:
{{ content }}

Return exactly one valid JSON object and nothing else:
{
  "decision": "ready|not_ready|insufficient_information",
  "is_ready_for_next_phase": true,
  "recommended_next_phase": "Exact next phase label or null",
  "rationale": "Short investment-team rationale tied to the available evidence",
  "blocking_gaps": ["Specific missing items or reasons blocking advancement"],
  "evidence_signals": ["Concrete positive or negative signals from the existing deal record"]
}

Rules:
- Base the answer only on the supplied saved deal context. Do not invent new facts.
- If evidence is insufficient, use "insufficient_information".
- `recommended_next_phase` must be either the provided expected next phase or null.
- `blocking_gaps` and `evidence_signals` must be arrays of strings.
- Keep the rationale concise and decision-useful."""


class DealPhaseReadinessService:
    @staticmethod
    def ensure_skill() -> AISkill:
        skill, _ = AISkill.objects.update_or_create(
            name=PHASE_READINESS_SKILL_NAME,
            defaults={
                "description": "Quick recommendation on whether a deal is ready for the next phase.",
                "prompt_template": PHASE_READINESS_PROMPT,
                "output_schema": {
                    "decision": "ready|not_ready|insufficient_information",
                    "is_ready_for_next_phase": "boolean",
                    "recommended_next_phase": "string|null",
                    "rationale": "string",
                    "blocking_gaps": ["string"],
                    "evidence_signals": ["string"],
                },
            },
        )
        return skill

    @staticmethod
    def get_expected_next_phase(current_phase: str | None) -> str | None:
        if not current_phase:
            return None
        try:
            current_index = ORDERED_DEAL_PHASES.index(current_phase)
        except ValueError:
            return None
        if current_index >= len(ORDERED_DEAL_PHASES) - 1:
            return None
        return ORDERED_DEAL_PHASES[current_index + 1]

    @staticmethod
    def build_context(deal: Deal) -> str:
        latest_analysis = deal.latest_analysis
        phase_history = [
            {
                "from_phase": log.from_phase,
                "to_phase": log.to_phase,
                "rationale": log.rationale,
                "changed_at": log.changed_at.isoformat() if log.changed_at else None,
            }
            for log in deal.phase_logs.all().order_by("-changed_at")[:5]
        ]

        payload = {
            "deal": {
                "id": str(deal.id),
                "title": deal.title,
                "priority": deal.priority,
                "current_phase": deal.current_phase,
                "expected_next_phase": DealPhaseReadinessService.get_expected_next_phase(deal.current_phase),
                "industry": deal.industry,
                "sector": deal.sector,
                "funding_ask": deal.funding_ask,
                "funding_ask_for": deal.funding_ask_for,
                "city": deal.city,
                "state": deal.state,
                "country": deal.country,
                "themes": deal.themes if isinstance(deal.themes, list) else [],
                "priority_rationale": deal.priority_rationale,
                "deal_summary": deal.deal_summary,
                "ambiguities": deal.ambiguities if isinstance(deal.ambiguities, list) else [],
                "deal_flow_decisions": deal.deal_flow_decisions or {},
                "rejection_stage_id": deal.rejection_stage_id,
                "rejection_reason": deal.rejection_reason,
                "management_meeting": deal.management_meeting,
                "business_proposal_stage": deal.business_proposal_stage,
                "ic_stage": deal.ic_stage,
            },
            "latest_analysis": {
                "version": latest_analysis.version if latest_analysis else None,
                "thinking": latest_analysis.thinking if latest_analysis else None,
                "analysis_json": latest_analysis.analysis_json if latest_analysis else {},
            },
            "recent_phase_history": phase_history,
        }
        return json.dumps(payload, default=str, indent=2)

    @staticmethod
    def normalize_result(result: dict | None, deal: Deal) -> dict:
        data = result if isinstance(result, dict) else {}
        expected_next_phase = DealPhaseReadinessService.get_expected_next_phase(deal.current_phase)

        decision = str(data.get("decision") or "insufficient_information").strip().lower()
        if decision not in {"ready", "not_ready", "insufficient_information"}:
            decision = "insufficient_information"

        is_ready = data.get("is_ready_for_next_phase")
        if isinstance(is_ready, bool):
            normalized_ready = is_ready
        else:
            normalized_ready = decision == "ready"

        recommended_next_phase = data.get("recommended_next_phase")
        if not isinstance(recommended_next_phase, str) or not recommended_next_phase.strip():
            recommended_next_phase = expected_next_phase if normalized_ready else None
        elif expected_next_phase and recommended_next_phase.strip() != expected_next_phase:
            recommended_next_phase = expected_next_phase
        else:
            recommended_next_phase = recommended_next_phase.strip()

        rationale = data.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip():
            rationale = "The model did not return a usable rationale from the saved deal context."

        def normalize_list(value: Any) -> list[str]:
            if not isinstance(value, list):
                return []
            return [str(item).strip() for item in value if str(item).strip()]

        return {
            "decision": decision,
            "is_ready_for_next_phase": normalized_ready,
            "recommended_next_phase": recommended_next_phase,
            "rationale": rationale.strip(),
            "blocking_gaps": normalize_list(data.get("blocking_gaps")),
            "evidence_signals": normalize_list(data.get("evidence_signals")),
        }

    @staticmethod
    def serialize_audit_log(log: AIAuditLog | None) -> dict | None:
        if not log:
            return None
        return {
            "audit_log_id": str(log.id),
            "status": log.status,
            "created_at": log.created_at.isoformat() if log.created_at else None,
            "error_message": log.error_message,
            "parsed_json": log.parsed_json if isinstance(log.parsed_json, dict) else None,
            "raw_thinking": log.raw_thinking,
        }

