from types import SimpleNamespace
from unittest.mock import MagicMock

from django.test import TestCase, SimpleTestCase

from ai_orchestrator.models import DocumentChunk
from ai_orchestrator.services.report_competitor_evidence import saved_competitor_blocks
from deals.models import Deal, VentureIntelligenceCompanyProfile, VentureIntelligenceCompanyRelation
from deals.services.analysis_section_rewrite import AnalysisSectionRewriteService


class SavedCompetitorEvidenceTests(TestCase):
    def test_search_results_and_enriched_profiles_are_scoped_to_the_current_deal(self):
        deal = Deal.objects.create(title='Target', competitor_candidates=[
            {'name': 'Peer', 'core_business': 'Consumer goods', 'evidence_urls': ['https://example.com/peer']},
            {'name': 'Unsourced', 'evidence_urls': []},
        ])
        other = Deal.objects.create(title='Other')
        profile = VentureIntelligenceCompanyProfile.objects.create(name='Peer')
        VentureIntelligenceCompanyRelation.objects.create(deal=deal, company_profile=profile, relation_type='competitor')
        DocumentChunk.objects.create(deal=deal, source_type='extracted_source', source_id=f'screener_{profile.id}', content='FY25 revenue INR 120 crore')
        DocumentChunk.objects.create(deal=other, source_type='extracted_source', source_id=f'screener_{profile.id}', content='Other-deal-only evidence')
        blocks = saved_competitor_blocks(deal, 'Executive Summary', max_tokens=6000)
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0]['url'], 'https://example.com/peer')
        self.assertIn('INR 120 crore', blocks[1]['text'])
        self.assertNotIn('Other-deal-only', str(blocks))
        self.assertEqual(saved_competitor_blocks(deal, 'Key Financials', max_tokens=6000), [])
        self.assertEqual(saved_competitor_blocks(deal, 'Executive Summary', max_tokens=1), [])


class ExecutiveSummaryRewriteContextTests(SimpleTestCase):
    def test_rewrite_uses_the_complete_other_report_sections(self):
        report = '## Executive Summary\n\nOld summary\n\n## Company Details\n\n' + ('Middle evidence\n\n' * 3000) + '## Next Steps\n\nFinal gate'
        context = AnalysisSectionRewriteService._report_context(report, 'Executive Summary')
        self.assertIn('Final gate', context)
        self.assertEqual(context.count('Middle evidence'), 3000)
        self.assertNotIn('Old summary', context)
