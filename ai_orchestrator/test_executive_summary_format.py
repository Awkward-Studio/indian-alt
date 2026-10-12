from django.test import SimpleTestCase
from ai_orchestrator.services.executive_summary_format import (
    EXECUTIVE_SUMMARY_HEADINGS, TABLE_HEADERS, SUBTABLE_HEADERS,
    executive_summary_format_errors, EXECUTIVE_SUMMARY_FORMAT,
)
from ai_orchestrator.services.report_prompt_quality import upgrade_report_prompt
from ai_orchestrator.services.report_sections import ICReportSectionService, ReportSectionStructureError


def table(headers):
    return '| '+' | '.join(headers)+' |\n| '+' | '.join('---' for _ in headers)+' |\n| '+' | '.join('Not provided' for _ in headers)+' |'


def fixture():
    sections = []
    for title in EXECUTIVE_SUMMARY_HEADINGS:
        body = table(TABLE_HEADERS[title]) if title in TABLE_HEADERS else 'Source evidence not provided.'
        if title == 'Key financials & metrics':
            body = '#### Financial performance\n\nIn native units; currency/scale Not provided.\n\n| Metric | FY24 Actual |\n| --- | --- |\n| Revenue | Not provided |\n| EBITDA margin | Not provided |\n\n#### Key metrics\n\n'+table(SUBTABLE_HEADERS['Key metrics'])
        if title == 'Industry dynamics':
            body = '\n\n'.join('#### '+h+'\n\n'+table(SUBTABLE_HEADERS[h]) for h in ['Market size by sub-sector','Market sizing','Sub-sector dynamics'])
        sections.append('### '+title+'\n\n'+body)
    return '## Executive Summary\n\n'+'\n\n'.join(sections)


class ExecutiveSummaryFormatTests(SimpleTestCase):
    def test_incomplete_industry_response_identifies_each_missing_final_page_block(self):
        incomplete = fixture().split('#### Market sizing')[0]+'| Phygit'
        errors = executive_summary_format_errors(incomplete)
        for heading in ['Key questions for founders', 'Key takeaways from calls', 'Similar companies we have seen in the space']:
            self.assertIn(heading, errors[0])
        self.assertIn('Missing headings:', errors[0])
        self.assertTrue(any('Market sizing' in error and 'Sub-sector dynamics' in error for error in errors))

    def test_all_three_pages_and_required_tables_accept_missing_source_data(self):
        self.assertEqual(executive_summary_format_errors(fixture()), [])

    def test_missing_heading_and_table_axis_are_rejected(self):
        self.assertTrue(executive_summary_format_errors(fixture().replace('### Funding','### Transaction snapshot')))
        errors = executive_summary_format_errors(fixture().replace('| Risk | Impact | Mitigant / diligence check |','| Risk | Mitigant | Impact |'))
        self.assertTrue(any('Key risks' in e for e in errors))

    def test_hosted_metric_label_variations_preserve_units_and_values(self):
        draft = fixture().replace('\n| Revenue |', '\n| **Revenue (INR Cr)** |').replace('\n| EBITDA margin |', '\n| EBITDA Margin (%) |')
        self.assertEqual(executive_summary_format_errors(draft), [])
        normalized = ICReportSectionService._normalize_section('Executive Summary', draft, strict_executive_summary=True)
        self.assertIn('Revenue (INR Cr)', normalized)

    def test_incorrect_financial_metric_and_column_shapes_still_fail(self):
        for draft in [fixture().replace('\n| Revenue |', '\n| Gross profit |'),
                      fixture().replace('| EBITDA margin | Not provided |', ''),
                      fixture().replace('| Revenue | Not provided |', '| Revenue | Not provided | Extra |'),
                      fixture().replace('\n| Revenue |', '\n| EBITDA margin |').replace('| EBITDA margin | Not provided |\n\n', '| Revenue | Not provided |\n\n')]:
            self.assertTrue(any('exactly two metric rows' in error for error in executive_summary_format_errors(draft)))

    def test_generator_enforces_presentation_contract_instead_of_publishing_wrong_layout(self):
        with self.assertRaises(ReportSectionStructureError):
            ICReportSectionService._normalize_section('Executive Summary','Old prose summary without the required three presentation pages.',strict_executive_summary=True)
        self.assertIn('### Industry dynamics',ICReportSectionService._normalize_section('Executive Summary',fixture(),strict_executive_summary=True))

    def test_upgrade_is_idempotent_and_uses_reference_structure_not_sample_facts(self):
        system, user = upgrade_report_prompt('System','## Executive Summary\nEvidence: {{ content }}',section_title='Executive Summary')
        self.assertIn(EXECUTIVE_SUMMARY_FORMAT,user)
        self.assertIn('EV / Gross profit', user)
        self.assertNotIn('68.3', user)
        self.assertEqual(upgrade_report_prompt(system,user,section_title='Executive Summary'),(system,user))
