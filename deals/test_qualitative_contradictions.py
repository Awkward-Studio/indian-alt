import json
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, TestCase

from deals.models import Deal, DealContradiction, DealDocument, VentureIntelligenceCompanyProfile, VentureIntelligenceCompanyRelation
from deals.services.contradiction_detection import ClaimEvidence, ContradictionDetectionService, DiscrepancyClassifier
from meetings.models import MeetingNote


class QualitativeClaimTests(SimpleTestCase):
    def extract(self, text, source_id='deck'):
        return ContradictionDetectionService.extract_qualitative_claims(text, subject='Acme', evidence=ClaimEvidence(source_type='deal_document', source_id=source_id, source_label='Deck', passage=''))

    def test_explicit_assertions_keep_exact_source_quote_and_period(self):
        text = 'FY2024: The company has pending litigation.'
        claim = self.extract(text)[0]
        self.assertEqual(claim.metric, 'pending_litigation')
        self.assertEqual(claim.value_text, 'affirmed')
        self.assertEqual(claim.unit, 'factual_assertion')
        self.assertEqual(claim.period, 'FY2024')
        self.assertEqual(claim.evidence.passage, text)

    def test_forecast_and_absence_are_not_factual_denials(self):
        for text in ('The company may have pending litigation.', 'Regulatory approval was discussed.', 'If the company has pending litigation, disclose it.'):
            self.assertEqual(self.extract(text), [])

    def test_opposing_assertions_form_candidate_without_numeric_interpretation(self):
        left = self.extract('FY2024: Financial statements were audited.', 'deck')[0]
        right = self.extract('FY2024: Financial statements were not audited.', 'call')[0]
        pair = ContradictionDetectionService.build_comparison_candidates([left, right])[0]
        classifier = DiscrepancyClassifier(llm_service=MagicMock(), model='test')
        payload = classifier._classification_payload(pair.left, pair.right)
        self.assertEqual(payload['claim_kind'], 'qualitative')
        self.assertNotIn('normalized_delta', payload)

    @patch('deals.services.contradiction_detection.PipelineRegistryService.render_prompt_stage', return_value=('system', 'prompt', None))
    def test_undated_assertions_require_clarification(self, _stage):
        left = self.extract('The company has pending litigation.', 'deck')[0]
        right = self.extract('The company has no pending litigation.', 'call')[0]
        provider = MagicMock()
        provider.execute_standard.return_value = {'response': json.dumps({'classification': 'contradiction', 'confidence': 0.9, 'rationale': 'Opposite statements.', 'materiality': 'high'})}
        result = DiscrepancyClassifier(llm_service=provider, model='test').classify(left, right)
        self.assertEqual(result.classification, 'insufficient_evidence')


class QualitativeWorkflowTests(TestCase):
    def setUp(self):
        from django.core.management import call_command
        call_command('seed_ai_prompts', verbosity=0)

    def test_saved_document_meeting_and_public_sources_are_compared_and_persisted(self):
        deal = Deal.objects.create(title='Acme')
        left = 'FY2024: The company has no pending litigation.'
        right = 'FY2024: The company has pending litigation.'
        DealDocument.objects.create(deal=deal, title='Management deck', normalized_text=left)
        note = MeetingNote.objects.create(title='Promoter call', body=right)
        note.deals.add(deal)
        profile = VentureIntelligenceCompanyProfile.objects.create(name='Acme', business_description=right, website='https://example.test/acme')
        VentureIntelligenceCompanyRelation.objects.create(deal=deal, company_profile=profile, relation_type='target')
        provider = MagicMock()
        provider.execute_standard.return_value = {'response': json.dumps({'classification': 'contradiction', 'confidence': 0.9, 'rationale': 'Same period statements differ.', 'materiality': 'high'})}
        result = DiscrepancyClassifier(llm_service=provider, model='test').run_for_deal(deal)
        self.assertEqual(result['claims'], 3)
        self.assertEqual(result['persisted'], 2)
        for record in DealContradiction.objects.all():
            self.assertEqual(record.metric, 'pending_litigation')
            self.assertEqual(record.unit, 'factual_assertion')
            self.assertEqual(record.left_claim['evidence']['passage'], left)
            self.assertEqual(record.right_claim['evidence']['passage'], right)
            self.assertEqual(record.review_status, 'UNREVIEWED')
