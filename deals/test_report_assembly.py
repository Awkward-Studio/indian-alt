import json
import re

from django.test import SimpleTestCase

from ai_orchestrator.prompt_contracts import IC_SECTION_TITLES
from deals.models import AnalysisKind
from deals.services.deal_creation import DealCreationService
from deals.services.report_assembly import clean_report_text, merge_report_text

SEPARATOR = '\n\n--- Supplemental Update ---\n\n'


class ReportAssemblyTests(SimpleTestCase):
    def test_legacy_snapshot_recovers_latest_sections_without_serialized_payloads(self):
        old = '## Executive Summary\nOld summary.'
        payload = json.dumps({'deal_model_data': {'title': 'Fixture'}, 'metadata': {}})
        latest = '\n\n'.join(f'## {title}\nLatest evidence {index}.' for index, title in enumerate(IC_SECTION_TITLES))
        cleaned = clean_report_text(SEPARATOR.join([old, payload, payload, latest]))
        self.assertEqual(cleaned, latest)
        self.assertNotIn('deal_model_data', cleaned)
        self.assertNotIn('Supplemental Update ---', cleaned)
        self.assertEqual(cleaned.count('## Executive Summary'), 1)

    def test_supplemental_section_replaces_its_previous_version_without_dropping_other_sections(self):
        previous = '## Executive Summary\nOld summary.\n\n## Company Details\nKeep evidence.'
        current = '## Executive Summary\nNew summary.\n\n### Scorecard\n| Revenue | 72 |'
        merged = merge_report_text(previous, current)
        self.assertIn('New summary.', merged)
        self.assertIn('Keep evidence.', merged)
        self.assertIn('| Revenue | 72 |', merged)
        self.assertNotIn('Old summary.', merged)

    def test_normalization_never_stores_synthesis_json_as_analyst_prose(self):
        payload = {'deal_model_data': {'title': 'Fixture'}, 'metadata': {}}
        previous = {'analyst_report': '## Executive Summary\nKeep summary.'}
        normalized = DealCreationService.normalize_analysis_payload(
            {**payload, 'analyst_report': json.dumps(payload)},
            previous_snapshot=previous, analysis_kind=AnalysisKind.SUPPLEMENTAL,
        )
        self.assertEqual(normalized['analyst_report'], '')
        self.assertEqual(normalized['deal_model_data'], payload['deal_model_data'])
        self.assertEqual(normalized['canonical_snapshot']['analyst_report'], previous['analyst_report'])

    def test_extracts_report_text_from_json_envelopes(self):
        payload = json.dumps({'analyst_report': '## Executive Summary\nProse.', 'deal_model_data': {}})
        self.assertEqual(clean_report_text(payload), '## Executive Summary\nProse.')
        self.assertEqual(clean_report_text(f'```json\n{payload}\n```'), '## Executive Summary\nProse.')

    def test_preserves_business_json_and_unstructured_supplemental_notes(self):
        self.assertEqual(clean_report_text('{"revenue":72}'), '{"revenue":72}')
        cleaned = clean_report_text(SEPARATOR.join(['## Executive Summary\nSummary.', 'New evidence resolves a gap.']))
        self.assertIn('### Supplemental Updates\n\nNew evidence resolves a gap.', cleaned)
        self.assertEqual(clean_report_text(cleaned), cleaned)
        self.assertIn('resolves a gap', merge_report_text(cleaned, '## Executive Summary\nUpdated summary.'))

    def test_initial_report_replaces_previous_report_and_empty_updates_preserve_it(self):
        self.assertEqual(merge_report_text('Old.', 'New.', initial=True), 'New.')
        self.assertEqual(merge_report_text('Old.', ''), 'Old.')
        self.assertEqual(merge_report_text('Old.', 'Old.'), 'Old.')

    def test_latest_occurrence_wins_even_when_a_complete_report_is_repeated(self):
        original = '## Executive Summary\nOriginal.'
        self.assertEqual(clean_report_text(SEPARATOR.join([original, '## Executive Summary\nIntermediate.', original])), original)

    def test_incremental_notes_preserve_eleven_section_contract(self):
        report = '\n\n'.join(f'## {title}\nEvidence.' for title in IC_SECTION_TITLES)
        merged = merge_report_text(report, 'New document evidence.')
        self.assertEqual(re.findall(r'^## (.+)$', merged, re.MULTILINE), list(IC_SECTION_TITLES))
        self.assertIn('### Supplemental Updates\n\nNew document evidence.', merged)

    def test_complete_replacement_report_supersedes_earlier_brief_and_notes(self):
        latest = '\n\n'.join(f'## {title}\nLatest evidence.' for title in IC_SECTION_TITLES)
        self.assertEqual(clean_report_text(SEPARATOR.join(['Old initial brief.', '## Executive Summary\nOld summary.', latest])), latest)
