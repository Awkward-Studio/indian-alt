from django.test import TestCase, override_settings
from unittest.mock import patch, MagicMock
from ai_orchestrator.models import AIAuditLog
from deals.models import Deal
from deals.services.report_coverage import report_coverage_for_deal
from deals.services.report_status import report_status_for_deal
from deals.services.report_coverage import latest_review_feedback

class ReportCoverageTests(TestCase):
    def setUp(self):
        self.deal = Deal.objects.create(title='Coverage target')
        self.parent = AIAuditLog.objects.create(source_id=str(self.deal.id), source_type='vdr_indexing',
            status='PROCESSING', source_metadata={'queue_kind': 'report', 'queue_state': 'active'})

    def review(self, gaps, status='COMPLETED'):
        return AIAuditLog.objects.create(source_id=str(self.parent.id), source_type='report_section_quality_review',
            status=status, source_metadata={'report_section': 'Transaction Details'}, parsed_json={'coverage_gaps': gaps})

    def test_open_and_addressed_history_survives_retries(self):
        self.review(['Explain dilution'])
        self.assertEqual(report_coverage_for_deal(self.deal)['open_count'], 1)
        self.review([])
        result = report_coverage_for_deal(self.deal)
        self.assertEqual(result['open_count'], 0)
        self.assertEqual(result['addressed_count'], 1)
        self.assertEqual(result['sections'][0]['gaps'][0]['text'], 'Explain dilution')

    def test_changed_gap_wording_is_not_claimed_to_be_addressed(self):
        self.review(['Explain dilution'])
        self.review(['Discuss dilution sensitivity'])
        result = report_coverage_for_deal(self.deal)
        self.assertEqual(result['open_count'], 1)
        self.assertEqual(result['addressed_count'], 0)
        self.assertEqual(result['sections'][0]['gaps'][0]['status'], 'earlier')

    def test_failed_review_and_another_deal_cannot_clear_gaps(self):
        self.review(['Explain dilution'])
        self.review([], status='FAILED')
        other = Deal.objects.create(title='Other target')
        self.assertIsNone(report_coverage_for_deal(other))
        self.assertEqual(report_coverage_for_deal(self.deal)['open_count'], 1)

    def test_new_run_does_not_inherit_old_open_gaps(self):
        self.review(['Explain dilution'])
        new = AIAuditLog.objects.create(source_id=str(self.deal.id), source_type='vdr_indexing',
            status='PENDING', source_metadata={'queue_kind': 'report', 'queue_state': 'queued'})
        result = report_coverage_for_deal(self.deal)
        self.assertEqual(result['report_audit_id'], str(new.id))
        self.assertEqual(result['open_count'], 0)
        self.assertEqual(result['review_count'], 0)

    def test_durable_report_status_remains_processing_for_live_refresh(self):
        self.assertEqual(report_status_for_deal(self.deal)['state'], 'processing')

    def review_with_both_feedbacks(self):
        review = self.review(['Explain dilution'])
        review.parsed_json['findings'] = [{'severity': 'error', 'claim': 'FY27 infusion',
            'issue': 'Incorrect period', 'correction': 'Use the own-sheet FY26 label', 'sources': ['R001']}]
        review.save(update_fields=['parsed_json'])
        return review

    @override_settings(VDR_DURABLE_QUEUE_ENABLED=True)
    @patch('deals.services.vdr_queue.kick')
    @patch('deals.services.folder_analysis.FolderAnalysisService.deal_analysis_readiness', return_value={'ready': True})
    def test_regeneration_snapshots_both_feedbacks(self, *_):
        from deals.services.folder_analysis import FolderAnalysisService
        self.review_with_both_feedbacks()
        self.parent.status = 'FAILED'
        self.parent.save(update_fields=['status'])
        result = FolderAnalysisService.trigger_vdr_analysis(self.deal, force_regenerate=True)
        queued = AIAuditLog.objects.get(id=result['audit_log_id'])
        feedback = queued.source_metadata['regeneration_feedback']['Transaction Details']
        self.assertEqual(feedback['coverage_gaps'], ['Explain dilution'])
        self.assertEqual(feedback['source_errors'][0]['correction'], 'Use the own-sheet FY26 label')

    def test_latest_clean_review_does_not_reuse_rejected_feedback(self):
        self.review_with_both_feedbacks()
        review = self.review([])
        review.parsed_json['findings'] = []
        review.save(update_fields=['parsed_json'])
        feedback = latest_review_feedback(self.deal)['Transaction Details']
        self.assertEqual(feedback['source_errors'], [])
        self.assertEqual(feedback['coverage_gaps'], [])

    @patch('deals.services.analysis_section_rewrite.AnalysisSectionRewriteService._meeting_context', return_value=('', {}))
    @patch('deals.services.analysis_section_rewrite.AnalysisSectionRewriteService._news_context', return_value=('', {}))
    @patch('deals.services.analysis_section_rewrite.AnalysisSectionRewriteService._document_context', return_value=('', {}))
    def test_section_rewrite_receives_both_feedbacks_and_user_instruction(self, *_):
        from deals.services.analysis_section_rewrite import AnalysisSectionRewriteService
        from ai_orchestrator.services.pipeline_registry import PipelineRegistryService
        PipelineRegistryService.ensure_report_pipeline_defaults()
        self.review_with_both_feedbacks()
        ai = MagicMock()
        ai.process_content.return_value = {'response': '## Transaction Details\nNew draft'}
        AnalysisSectionRewriteService(ai).rewrite(deal=self.deal, section_title='Transaction Details',
            section_markdown='## Transaction Details\nOld draft', instruction='Keep it concise',
            full_report='## Transaction Details\nOld draft')
        metadata = ai.process_content.call_args.kwargs['metadata']
        self.assertIn('Keep it concise', metadata['rewrite_instruction'])
        self.assertIn('Explain dilution', metadata['rewrite_instruction'])
        self.assertIn('own-sheet FY26', metadata['rewrite_instruction'])
