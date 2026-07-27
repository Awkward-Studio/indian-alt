import logging
from django.db import transaction
from deals.models import Deal, DealPhaseLog

logger = logging.getLogger(__name__)


class DealFlowValidationError(ValueError):
    pass


class DealFlowService:
    """
    Domain Service for handling Deal Flow state transitions and 
    recording Phase Logs securely.
    """

    @staticmethod
    def _effective_pass_reason(deal: Deal, reason: str | None) -> str:
        candidates = [
            reason,
            getattr(deal, "reasons_for_passing", None),
            getattr(deal, "rejection_reason", None),
        ]
        for candidate in candidates:
            normalized = str(candidate or "").strip()
            if normalized:
                return normalized
        raise DealFlowValidationError("A non-whitespace reason is required before passing a deal.")

    @staticmethod
    @transaction.atomic
    def transition_phase(deal: Deal, to_phase: str, rationale: str = None, request_user=None) -> dict:
        """
        Transitions a deal to a new phase and logs the rationale.
        """
        from_phase = getattr(deal, 'current_phase', None)
        update_fields = ['current_phase', 'deal_status']
        if to_phase == "Passed":
            rationale = DealFlowService._effective_pass_reason(deal, rationale)
            deal.reasons_for_passing = rationale
            deal.rejection_reason = rationale
            update_fields.extend(['reasons_for_passing', 'rejection_reason'])
        
        # Update the Deal
        deal.current_phase = to_phase
        deal.deal_status = to_phase
        deal.save(update_fields=update_fields)
        
        # Log the transition
        user_profile = request_user.profile if (request_user and hasattr(request_user, 'profile')) else None
        DealPhaseLog.objects.create(
            deal=deal,
            from_phase=from_phase,
            to_phase=to_phase,
            rationale=rationale,
            changed_by=user_profile
        )
        
        return {
            "status": "success",
            "from_phase": from_phase,
            "to_phase": to_phase,
            "deal_status": deal.deal_status,
        }

    @staticmethod
    @transaction.atomic
    def update_flow_state(deal: Deal, active_stage: str = None, decisions_update: dict = None, reason: str = None, rejection_stage_id: str = None, request_user=None) -> dict:
        """
        Unified handler for the 18-stage interactive deal flow.
        Updates decisions, phases, and rejection tracking.
        """
        if active_stage == "Passed":
            reason = DealFlowService._effective_pass_reason(deal, reason)
            deal.reasons_for_passing = reason

        # 1. Update Decisions (Allow reset if explicitly empty dict)
        if decisions_update is not None:
            if decisions_update == {}:
                deal.deal_flow_decisions = {}
            else:
                current_decisions = deal.deal_flow_decisions or {}
                current_decisions.update(decisions_update)
                deal.deal_flow_decisions = current_decisions
        
        # 2. Update active stage & create log if changed
        if active_stage and deal.current_phase != active_stage:
            from_phase = deal.current_phase
            deal.current_phase = active_stage
            deal.deal_status = active_stage
            
            user_profile = request_user.profile if (request_user and hasattr(request_user, 'profile')) else None
            DealPhaseLog.objects.create(
                deal=deal,
                from_phase=from_phase,
                to_phase=active_stage,
                rationale=reason,
                changed_by=user_profile
            )
            
        elif active_stage:
            deal.deal_status = active_stage

        # 3. Rejection tracking
        if rejection_stage_id is not None:
            deal.rejection_stage_id = rejection_stage_id
            deal.rejection_reason = reason

        deal.save(update_fields=[
            'deal_flow_decisions', 'current_phase', 'deal_status',
            'rejection_stage_id', 'rejection_reason', 'reasons_for_passing',
        ])
        
        return {
            "status": "success",
            "current_phase": deal.current_phase,
            "deal_status": deal.deal_status,
            "deal_flow_decisions": deal.deal_flow_decisions
        }
