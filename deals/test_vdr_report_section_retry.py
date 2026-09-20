from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from ai_orchestrator.services.report_sections import (
    ReportSectionDegenerateOutputError,
    ReportSectionValidationError,
)
from deals.tasks import process_vdr_report_section


class VDRReportSectionRetryTests(SimpleTestCase):
    def test_degenerate_repetition_retries_model_request(self):
        audit = Mock(source_metadata={})
        with (
            patch("deals.services.vdr_queue.delivery_is_current", return_value=True),
            patch("deals.services.vdr_queue.heartbeat"),
            patch("deals.services.vdr_queue.start_heartbeat"),
            patch("ai_orchestrator.models.AIAuditLog.objects.get", return_value=audit),
            patch("deals.tasks.Deal.objects.get", return_value=Mock()),
            patch(
                "deals.tasks._durable_report_foundation",
                return_value=({"deal_model_data": {}}, [], None, None),
            ),
            patch(
                "ai_orchestrator.services.report_section_evidence."
                "ICReportSectionEvidenceService.retrieve",
                return_value={"context": "Ranked evidence", "citations": {"1": {}}},
            ),
            patch(
                "ai_orchestrator.services.report_sections."
                "ICReportSectionService._generate_section",
                side_effect=ReportSectionDegenerateOutputError("Repeated citation loop."),
            ),
            patch.object(
                process_vdr_report_section,
                "retry",
                side_effect=RuntimeError("retry scheduled"),
            ) as retry,
            self.assertRaisesRegex(RuntimeError, "retry scheduled"),
        ):
            process_vdr_report_section.run(
                deal_id="deal-1",
                audit_log_id="audit-1",
                section_title="Industry Overview",
                queue_generation=2,
            )

        retry.assert_called_once()

    def test_validation_failure_does_not_repeat_model_request(self):
        audit = Mock(source_metadata={})
        with (
            patch("deals.services.vdr_queue.delivery_is_current", return_value=True),
            patch("deals.services.vdr_queue.heartbeat"),
            patch("deals.services.vdr_queue.start_heartbeat"),
            patch("ai_orchestrator.models.AIAuditLog.objects.get", return_value=audit),
            patch("deals.tasks.Deal.objects.get", return_value=Mock()),
            patch(
                "deals.tasks._durable_report_foundation",
                return_value=({"deal_model_data": {}}, [], None, None),
            ),
            patch(
                "ai_orchestrator.services.report_section_evidence."
                "ICReportSectionEvidenceService.retrieve",
                return_value={"context": "Ranked evidence", "citations": {"1": {}}},
            ),
            patch(
                "ai_orchestrator.services.report_sections."
                "ICReportSectionService._generate_section",
                side_effect=ReportSectionValidationError("Invalid citation marker."),
            ),
            patch.object(process_vdr_report_section, "retry") as retry,
        ):
            result = process_vdr_report_section.run(
                deal_id="deal-1",
                audit_log_id="audit-1",
                section_title="Company Details",
                queue_generation=2,
            )

        self.assertEqual(result, {"status": "failed", "error": "Invalid citation marker."})
        retry.assert_not_called()
