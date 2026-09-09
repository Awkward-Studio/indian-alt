"""Shared checks for the canonical analyst report shown in the deal ledger."""

from __future__ import annotations

from ai_orchestrator.services.report_sections import ICReportSectionService


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
