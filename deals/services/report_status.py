"""Shared checks for the canonical analyst report shown in the deal ledger."""

from __future__ import annotations

from ai_orchestrator.services.report_sections import ICReportSectionService


REPORT_ACTIVE_STATUSES = ("PENDING", "PROCESSING")


def _latest_analysis(deal):
    prefetched = getattr(deal, "prefetched_report_analyses", None)
    if prefetched is not None:
        return prefetched[0] if prefetched else None
    return deal.analyses.order_by("-version", "-created_at").first()


def _latest_report_audit(deal, statuses=None):
    from ai_orchestrator.models import AIAuditLog

    queryset = AIAuditLog.objects.filter(
        source_type="deal_full_synthesis",
        source_id=str(deal.id),
    ).order_by("-created_at")
    if statuses:
        queryset = queryset.filter(status__in=statuses)
    return queryset.first()


def _latest_active_section_audit(deal):
    """Find a section fallback still running under a synthesis audit.

    Older workers could mark the parent model request complete before the
    section-by-section completion pass finished. Treat those child requests as
    active too, so clients never offer a duplicate report while sections are
    still being generated.
    """
    from ai_orchestrator.models import AIAuditLog

    parent_ids = list(AIAuditLog.objects.filter(
        source_type="deal_full_synthesis",
        source_id=str(deal.id),
    ).values_list("id", flat=True))
    if not parent_ids:
        return None
    return AIAuditLog.objects.filter(
        source_type__in=["email_report_section", "vdr_report_section"],
        source_id__in=[str(parent_id) for parent_id in parent_ids],
        status__in=REPORT_ACTIVE_STATUSES,
    ).order_by("-created_at").first()


def report_status_for_deal(deal) -> dict:
    """Return the persisted report state used by both report entry points."""
    active = _latest_report_audit(deal, REPORT_ACTIVE_STATUSES)
    active = active or _latest_active_section_audit(deal)
    latest = _latest_analysis(deal)
    latest_error = _latest_report_audit(deal, ("FAILED",))

    if active:
        return {
            "state": "processing",
            "analysis_id": str(latest.id) if latest else None,
            "version": latest.version if latest else None,
            "generated_at": latest.created_at.isoformat() if latest else None,
            "audit_log_id": str(active.id),
            "error": None,
        }

    if latest is None:
        return {
            "state": "not_started",
            "analysis_id": None,
            "version": None,
            "generated_at": None,
            "audit_log_id": str(latest_error.id) if latest_error else None,
            "error": latest_error.error_message if latest_error else None,
        }

    if not is_complete_analyst_report(latest.analysis_json):
        return {
            "state": "needs_regeneration",
            "analysis_id": str(latest.id),
            "version": latest.version,
            "generated_at": latest.created_at.isoformat() if latest.created_at else None,
            "audit_log_id": str(latest_error.id) if latest_error else None,
            "error": latest_error.error_message if latest_error else "The stored analyst report is incomplete.",
        }

    # A completed report is stale once a source document or linked email has
    # changed after the report was generated.
    latest_evidence_at = latest.created_at
    documents = getattr(deal, "prefetched_report_documents", None)
    if documents is None:
        # DealDocument has no updated_at column. Load the timestamps that can
        # change when extraction or chunking refreshes its evidence instead of
        # asking Django for a field that does not exist.
        documents = deal.documents.only(
            "created_at", "last_transcribed_at", "last_chunked_at",
        ).all()
    emails = getattr(deal, "prefetched_report_emails", None)
    if emails is None:
        emails = deal.emails.only("updated_at").all()
    for source in [*documents, *emails]:
        timestamps = [
            getattr(source, "updated_at", None),
            getattr(source, "last_transcribed_at", None),
            getattr(source, "last_chunked_at", None),
            getattr(source, "created_at", None),
        ]
        changed_at = max((timestamp for timestamp in timestamps if timestamp), default=None)
        if changed_at and (latest_evidence_at is None or changed_at > latest_evidence_at):
            return {
                "state": "needs_regeneration",
                "analysis_id": str(latest.id),
                "version": latest.version,
                "generated_at": latest.created_at.isoformat() if latest.created_at else None,
                "audit_log_id": str(latest_error.id) if latest_error else None,
                "error": "New or changed deal evidence is available.",
            }

    return {
        "state": "completed",
        "analysis_id": str(latest.id),
        "version": latest.version,
        "generated_at": latest.created_at.isoformat() if latest.created_at else None,
        "audit_log_id": None,
        "error": None,
    }


def analyst_report_from_payload(payload: object) -> str:
    """Return the report using the same top-level/canonical precedence as the UI."""
    if not isinstance(payload, dict):
        return ""

    report = payload.get("analyst_report")
    if isinstance(report, str) and report.strip():
        return report.strip()

    canonical = payload.get("canonical_snapshot")
    if isinstance(canonical, dict):
        report = canonical.get("analyst_report")
        if isinstance(report, str):
            return report.strip()
    return ""


def is_complete_analyst_report(payload: object) -> bool:
    """Require the exact ordered 11-section IC report contract."""
    return ICReportSectionService.is_complete(analyst_report_from_payload(payload))
