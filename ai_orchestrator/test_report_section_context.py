from django.test import SimpleTestCase

from ai_orchestrator.prompt_contracts import IC_SECTION_TITLES
from ai_orchestrator.services.report_section_context import prior_section_context


class ExecutiveSummaryReportContextTests(SimpleTestCase):
    def test_current_run_overrides_latest_report_and_missing_sections_fall_back(self):
        latest = '## Company Details\n\nOld company\n\n## Next Steps\n\nSaved actions'
        manifest = [{'title': 'Company Details', 'status': 'completed', 'content': '## Company Details\n\nCurrent company'}]
        context = prior_section_context({'report_section_queue': manifest}, 'Executive Summary', fallback_report=latest)
        self.assertIn('Current company', context)
        self.assertIn('Saved actions', context)
        self.assertNotIn('Old company', context)

    def test_summary_keeps_all_completed_sections_and_middle_paragraphs(self):
        manifest = []
        for title in IC_SECTION_TITLES:
            body = '\n\n'.join(f'{title} paragraph {index} [1]' for index in range(10))
            manifest.append({'title': title, 'status': 'completed',
                             'content': f'## {title}\n\n{body}\n\n### Citations\n\nOld citation footer'})
        context = prior_section_context({'report_section_queue': manifest}, 'Executive Summary', max_tokens=1)
        for title in IC_SECTION_TITLES:
            if title != 'Executive Summary':
                for index in range(10):
                    self.assertIn(f'{title} paragraph {index}', context)
        self.assertNotIn('Executive Summary paragraph', context)
        self.assertNotIn('Old citation footer', context)
        self.assertNotIn('[1]', context)

    def test_incomplete_sections_are_not_presented_as_completed_reports(self):
        context = prior_section_context({'report_section_queue': [
            {'title': 'Company Details', 'status': 'completed', 'content': 'Company report'},
            {'title': 'Risk Factors', 'status': 'failed', 'content': 'Failed report'},
        ]}, 'Executive Summary')
        self.assertIn('Company report', context)
        self.assertNotIn('Failed report', context)
