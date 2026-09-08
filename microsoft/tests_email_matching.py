from unittest.mock import patch
from django.test import TestCase
from deals.models import Deal
from microsoft.models import Email, EmailAccount
from microsoft.services.email_matching import EmailDecisionService as Decisions
from microsoft.services.email_contributions import EmailContributionParser


class EmailDecisionTests(TestCase):
    def setUp(self):
        self.account = EmailAccount.objects.create(email='matching@example.test')
        self.email = Email.objects.create(email_account=self.account, graph_id='match', conversation_id='thread')
        self.deal = Deal.objects.create(title='Acme Components')

    def test_both_types_use_exact_explicit_identity_without_ai(self):
        for body in ['Summary\nCall with team\nTranscript\nSales grew.', 'Please see revised terms.']:
            result = Decisions.match(self.email, 'deal_name=Acme Components\n' + body, Deal.objects.all(), use_ai=False)
            self.assertEqual(result['deal_id'], str(self.deal.id))

    def test_mailbox_scope_and_empty_thread_do_not_inherit_other_links(self):
        other = EmailAccount.objects.create(email='private@example.test')
        Email.objects.create(email_account=other, graph_id='private', conversation_id='thread', deal=self.deal)
        self.assertNotEqual(Decisions.match(self.email, 'hello', Deal.objects.all(), use_ai=False)['status'], 'matched')
        self.email.deal = self.deal
        self.email.save()
        self.assertEqual(Decisions.match(self.email, 'hello', Deal.objects.none(), use_ai=False)['status'], 'needs_review')

    def test_new_reply_with_quoted_meeting_keeps_segment_roles(self):
        source = {'body_text': 'Please update the financial model before Friday.\nFrom: a@example.test\nSent: Monday\nSummary\nMeeting held\nTranscript\nRevenue discussed'}
        parts = EmailContributionParser.parse(source, email_id=self.email.id)
        result = Decisions.classify(parts, source_id=self.email.id, use_ai=False)
        self.assertEqual(result['type'], 'NORMAL_EMAIL')
        self.assertIn('MEETING_NOTE', [r['type'] for r in result['segment_roles']])

    @patch.object(Decisions, 'ai', return_value={'deal_id': 'invented', 'evidence': 'Acme Components'})
    @patch.object(Decisions, 'candidates')
    def test_invented_candidate_cannot_link(self, candidates, ai):
        candidates.return_value = [{'deal_id': str(self.deal.id), 'title': self.deal.title}]
        with self.assertRaises(ValueError):
            Decisions.match(self.email, 'Acme Components update', Deal.objects.all())

    def test_duplicate_explicit_names_require_review(self):
        Deal.objects.create(title=self.deal.title)
        self.assertEqual(Decisions.match(self.email, 'deal_name=Acme Components', Deal.objects.all(), use_ai=False)['status'], 'needs_review')

    @patch.object(Decisions, 'ai')
    def test_new_deal_initializer_is_small_and_allowlisted(self, ai):
        ai.return_value = {
            'deal_model_data': {
                'title': 'New Components', 'sector': 'Industrials',
                'funding_ask': 'INR 50 crore', 'deal_summary': 'not allowed here',
            },
            'ambiguous_points': ['Exact legal name is unclear.'],
        }
        parts = EmailContributionParser.parse(
            {'body_text': 'New Components is raising INR 50 crore.'}, email_id=self.email.id,
        )

        result = Decisions.initialize(self.email, parts)

        self.assertEqual(result['deal_model_data']['title'], 'New Components')
        self.assertNotIn('deal_summary', result['deal_model_data'])
        self.assertEqual(result['metadata']['ambiguous_points'], ['Exact legal name is unclear.'])

    def test_vm_evidence_with_normalized_whitespace_maps_to_exact_source(self):
        source = 'I wanted to share details\non 3TenX.'
        self.assertEqual(
            Decisions._exact_excerpt(source, 'I wanted to share details on 3TenX.'),
            source,
        )
