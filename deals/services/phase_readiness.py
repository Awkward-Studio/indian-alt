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

PHASE_READINESS_PROMPT = """Evaluate whether this deal is ready to move to its next phase using the firm's 18-step deal process.

Input context:
{{ content }}

Your job:
- Read the deal's current_phase and expected_next_phase from the supplied context.
- Judge readiness only against the gate for the CURRENT phase.
- Recommend advancement only if the evidence in the saved deal record supports clearing that phase's gate.
- If the record is incomplete, say so instead of guessing.

Phase gates:
1: Deal Sourced
- Ready only if the deal appears within mandate based on available sector, geography, ticket size, stake, and fit signals.
- Block if mandate fit is unclear or available facts suggest out-of-mandate.

2: Initial Banker Call
- Ready only if available notes suggest acceptable deal dynamics, promoter profile, process quality, and preliminary valuation expectations.
- Block if valuation/process/promoter concerns appear unresolved or there is no evidence the call happened.

3: NDA Execution
- Ready only if there is evidence the NDA is signed/executed or confidential information has clearly been shared post-NDA.
- Block if NDA status is missing, delayed, or disputed.

4: Initial Materials Review
- Ready only if the available materials support a financially sound business, credible projections, and no immediate red flags around cap table, audit quality, or business fundamentals.
- Block if core materials are missing or early red flags are unresolved.

5: Financial Model Call
- Ready only if assumptions, unit economics, growth drivers, and return potential appear defensible from saved notes and analysis.
- Block if model credibility is weak, assumptions are unsupported, or there is no evidence the walkthrough happened.

6: Additional Data Request
- Ready only if requested follow-up data appears received and materially supports the thesis without contradiction.
- Block if key follow-up items are still missing, inconsistent, or withheld.

7: Industry Research
- Ready only if sector work supports a differentiated thesis, attractive market structure, and manageable regulatory/competitive risk.
- Block if thesis support is thin or the market evidence cuts against the deal.

8: Reference Calls
- Ready only if independent references support management credibility, moat, and market opportunity without surfacing material red flags.
- Block if references are absent, mixed, or negative on integrity, concentration, channel conflict, or market claims.

9: IA Model Build
- Ready only if the independent model supports fund-level return requirements and downside protection at the current entry assumptions.
- Block if return hurdles are not met, downside is unattractive, or the model is incomplete.

10: Field Visit
- Ready only if on-site observations reinforce management quality, operating discipline, and consistency with the data room.
- Block if there is no evidence of a visit or if observations raise execution/culture discrepancies.

11: Business Proposal
- Ready only if the investment team appears aligned to proceed and the deal is internally documented well enough for the next step.
- Block if internal consensus is missing, the thesis is not decision-ready, or major open issues remain.

12: Term Sheet
- Ready only if there is evidence of commercially acceptable agreement on valuation, governance, economics, and exit mechanics.
- Block if terms are unsigned, materially disputed, or misaligned.

13: Full Due Diligence
- Ready only if legal, financial/tax, and commercial diligence findings are either clean or adequately mitigated with no fundamental deal-breakers.
- Block if major diligence workstreams are incomplete or material findings remain unresolved.

14: IC Note I
- Ready only if the Stage I IC package appears complete, decision-ready, and aligned with diligence findings and policy requirements.
- Block if the IC materials are incomplete, uncirculated, or not yet approved.

15: IC Feedback
- Ready only if Stage I IC concerns appear substantively addressed and documented.
- Block if IC feedback remains open, partially answered, or unsupported by new work.

16: IC Note II
- Ready only if final IC approval appears supported, minuted, and backed by updated analysis on remaining issues.
- Block if final IC approval is absent or the case is still not fully resolved.

17: Definitive Documentation
- Ready only if definitive agreements are substantially finalized/executed and required regulatory approvals are obtained or clearly satisfied.
- Block if key documents, approvals, or negotiated protections remain outstanding.

18: Closure
- Ready only if conditions precedent are satisfied and the disbursement/closing record is complete.
- Block if any CPs, approvals, or final authorizations remain open.

Decision rules:
- Use "ready" only when the saved evidence is strong enough that an investment team could reasonably advance the deal now.
- Use "not_ready" when the evidence shows the phase gate has not been cleared.
- Use "insufficient_information" when the record does not contain enough evidence to judge the current phase properly.
- Be conservative. Missing critical evidence should usually lead to "insufficient_information" or "not_ready", not "ready".
- Do not judge the whole deal abstractly. Judge the specific gate for the current phase.
- Do not recommend skipping phases.
- `recommended_next_phase` must be the provided expected next phase or null.
- For `not_ready` and `insufficient_information`, `blocking_gaps` must contain the exact reasons the deal cannot advance beyond the current phase right now.
- Each `blocking_gaps` item must be specific, current-phase-aware, and decision-ready.
- Each `blocking_gaps` item should include the missing proof, unresolved issue, or failed condition that must be cleared.
- Do not use vague blockers like "more diligence needed" unless you name the exact missing diligence item.
- If the deal is `ready`, use an empty `blocking_gaps` array unless there is a narrowly scoped caveat that still does not prevent advancement.

Return exactly one valid JSON object and nothing else:
{
  "decision": "ready|not_ready|insufficient_information",
  "is_ready_for_next_phase": true,
  "recommended_next_phase": "Exact next phase label or null",
  "rationale": "Short investment-team rationale tied to the available evidence and the current phase gate",
  "blocking_gaps": ["Exact current-phase blockers with the missing proof, unresolved issue, or failed condition preventing advancement"],
  "evidence_signals": ["Concrete positive or negative signals from the saved deal record that are relevant to this phase"]
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
                "description": "Stage-aware recommendation on whether a deal is ready for the next phase, including exact blockers preventing advancement.",
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
        current_analysis = deal.current_analysis or {}
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
            "current_analysis": {
                "version": current_analysis.get("version"),
                "kind": current_analysis.get("kind"),
                "thinking": current_analysis.get("thinking"),
                "analysis_json": current_analysis.get("analysis_json") or {},
                "canonical_snapshot": current_analysis.get("canonical_snapshot") or {},
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

        blocking_gaps = normalize_list(data.get("blocking_gaps"))
        if decision in {"not_ready", "insufficient_information"} and not blocking_gaps:
            if decision == "not_ready":
                blocking_gaps = [
                    f"The saved deal context does not show that the gate for {deal.current_phase} has been cleared."
                ]
            else:
                blocking_gaps = [
                    f"The saved deal context is missing enough phase-specific evidence to determine whether {deal.current_phase} is cleared."
                ]

        return {
            "decision": decision,
            "is_ready_for_next_phase": normalized_ready,
            "recommended_next_phase": recommended_next_phase,
            "rationale": rationale.strip(),
            "blocking_gaps": blocking_gaps,
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
