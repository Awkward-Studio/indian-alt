from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from ai_orchestrator.services.report_sections import (
    ReportSectionDegenerateOutputError,
    ReportSectionTooShortError,
    ReportSectionValidationError,
)
from deals.tasks import process_vdr_report_section


class VDRReportSectionRetryTests(SimpleTestCase):
    def assert_retry_receives_draft(self, error, retries=1, feedback=None):
        from types import SimpleNamespace
        previous=SimpleNamespace(error_message=error,
            raw_response="## Executive Summary\n\nA supported finding [R001] and its investment implication.")
        query=Mock();query.exclude.return_value=query;query.order_by.return_value.first.return_value=previous
        audit=Mock(source_metadata={'regeneration_feedback': {'Executive Summary': feedback}} if feedback else {})
        with (
            patch.object(process_vdr_report_section.request,'retries',retries),
            patch('deals.services.vdr_queue.delivery_is_current',return_value=True),
            patch('deals.services.vdr_queue.heartbeat'),patch('deals.services.vdr_queue.start_heartbeat'),
            patch('ai_orchestrator.models.AIAuditLog.objects.get',return_value=audit),
            patch('ai_orchestrator.models.AIAuditLog.objects.filter',return_value=query),
            patch('deals.tasks.Deal.objects.get',return_value=Mock()),
            patch('deals.tasks._durable_report_foundation',return_value=({'deal_model_data':{}},[],None,None)),
            patch('ai_orchestrator.services.report_section_evidence.ICReportSectionEvidenceService.retrieve',
                return_value={'context':'Fresh primary evidence [R010]','citations':{'10':{}}}),
            patch('ai_orchestrator.services.report_sections.ICReportSectionService._generate_section',return_value='Completed section') as generate,
        ):
            result=process_vdr_report_section.run(deal_id='deal-1',audit_log_id='audit-1',section_title='Executive Summary',queue_generation=2)
        self.assertEqual(result['status'],'completed')
        evidence=generate.call_args.kwargs['evidence']
        self.assertIn('<draft_to_expand>',evidence)
        self.assertIn('A supported finding',evidence)
        self.assertIn('replace its citations',evidence)
        self.assertNotIn('[R001]',evidence.split('<draft_to_expand>')[1])
        return evidence

    def test_under_length_retry_receives_the_rejected_draft_and_fresh_citation_instruction(self):
        evidence = self.assert_retry_receives_draft("Report section 'Executive Summary' was too short: 900 words; minimum is 1260.")
        self.assertGreater(evidence.index('<required_draft_corrections>'), evidence.index('</draft_to_expand>'))
        self.assertIn('Word count no longer blocks acceptance', evidence)

    def test_citation_and_calculation_retry_corrections_follow_the_old_draft(self):
        evidence = self.assert_retry_receives_draft('No verifiable evidence citations')
        self.assertIn('Task / Exact Action or Why It Matters', evidence)
        self.assertGreater(evidence.index('<required_draft_corrections>'), evidence.index('</draft_to_expand>'))
        evidence = self.assert_retry_receives_draft('Report has inconsistent calculations')
        self.assertIn('entire power expression for IRR/CAGR', evidence)

    def test_statement_bridge_and_missing_source_inputs_get_specific_corrections(self):
        evidence = self.assert_retry_receives_draft('Key Financials arithmetic does not reconcile: EBIT')
        self.assertIn('PBT is a different metric', evidence)
        self.assertIn('unrounded operating EBITDA', evidence)
        evidence = self.assert_retry_receives_draft('Key Financials source values do not match: Cost of Goods Sold')
        self.assertIn('not estimates or interpolation', evidence)
        self.assertIn('Never copy a PBT cell into EBIT', evidence)

    def test_source_review_and_calculation_retries_receive_draft_with_feedback(self):
        for error in ['Source review rejected: incorrect fiscal year', 'Inconsistent calculation: EBITDA margin']:
            with self.subTest(error=error):
                evidence = self.assert_retry_receives_draft(error)
                self.assertIn(error, evidence)

    def test_frontend_regeneration_receives_previous_run_draft_and_both_feedbacks(self):
        evidence = self.assert_retry_receives_draft('Prior review rejection', retries=0, feedback={
            'report_audit_id': 'previous-report', 'coverage_gaps': ['Explain dilution'],
            'source_errors': [{'claim': 'FY27 infusion', 'issue': 'Wrong period', 'correction': 'Use FY26'}]})
        self.assertIn('Explain dilution', evidence)
        self.assertIn('Use FY26', evidence)

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
            patch(
                "ai_orchestrator.services.report_sections."
                "ICReportSectionService._mark_prior_rejected_attempts_retried",
            ) as mark_retried,
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
        mark_retried.assert_called_once_with(
            source_type="vdr_report_section", source_id="audit-1", title="Industry Overview",
        )

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

    def test_under_length_draft_retries_model_request(self):
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
                side_effect=ReportSectionTooShortError("Draft too short."),
            ),
            patch(
                "ai_orchestrator.services.report_sections."
                "ICReportSectionService._mark_prior_rejected_attempts_retried",
            ) as mark_retried,
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
                section_title="Transaction / Trading Multiples",
                queue_generation=2,
            )

        retry.assert_called_once()
        mark_retried.assert_called_once_with(
            source_type="vdr_report_section", source_id="audit-1", title="Transaction / Trading Multiples",
        )
