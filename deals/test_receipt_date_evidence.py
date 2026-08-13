from datetime import date, datetime, timezone as datetime_timezone

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Profile
from deals.models import Deal, DealReceiptDateAudit, DealReceiptDateSuggestion
from deals.services.receipt_date_evidence import ReceiptDateEvidenceService
from microsoft.models import Email, EmailAccount


class ReceiptDateEvidenceServiceTests(TestCase):
    def test_proposals_are_idempotent_and_conflicting_dates_are_explicit(self):
        deal = Deal.objects.create(title='Evidence Co')

        first, created = ReceiptDateEvidenceService.propose(
            deal=deal,
            proposed_date=date(2026, 1, 4),
            source_type='WORKBOOK',
            source_id='fund-i.xlsx:row:7',
            evidence={'cell': 'B7'},
        )
        duplicate, duplicate_created = ReceiptDateEvidenceService.propose(
            deal=deal,
            proposed_date=date(2026, 1, 4),
            source_type='WORKBOOK',
            source_id='fund-i.xlsx:row:7',
            evidence={'cell': 'B7'},
        )

        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(first.id, duplicate.id)
        self.assertEqual(first.status, DealReceiptDateSuggestion.Status.PENDING)

        ReceiptDateEvidenceService.propose(
            deal=deal,
            proposed_date=date(2026, 1, 5),
            source_type='ANALYST',
            source_id='meeting-note:14',
            evidence={'description': 'Dated meeting note'},
        )

        self.assertEqual(
            set(deal.receipt_date_suggestions.values_list('status', flat=True)),
            {DealReceiptDateSuggestion.Status.CONFLICT},
        )

    def test_linked_email_uses_graph_received_time_and_never_deal_created_at(self):
        received = datetime(2026, 2, 8, 21, 30, tzinfo=datetime_timezone.utc)
        account = EmailAccount.objects.create(email='inbox@example.com')
        Email.objects.create(
            email_account=account,
            graph_id='graph-message-1',
            subject='New opportunity',
            date_received=received,
        )
        deal = Deal.objects.create(title='Email evidence', source_email_id='graph-message-1')

        suggestion, created = ReceiptDateEvidenceService.propose_from_linked_email(deal)

        self.assertTrue(created)
        self.assertEqual(suggestion.proposed_date, received.date())
        self.assertEqual(suggestion.evidence['date_received'], received.isoformat())
        self.assertNotEqual(suggestion.proposed_date, deal.created_at.date())


class ReceiptDateEvidenceApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='date-reviewer', password='password')
        self.profile = Profile.objects.create(
            user=self.user,
            name='Date Reviewer',
            email='date-reviewer@example.com',
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _deal(self, title='Undated Deal', fund='FUND1'):
        deal = Deal.objects.create(title=title, fund=fund)
        deal.responsibility.add(self.profile)
        return deal

    def _suggest(self, deal, proposed_date=date(2026, 3, 10), source_id='source:1'):
        return ReceiptDateEvidenceService.propose(
            deal=deal,
            proposed_date=proposed_date,
            source_type='ANALYST',
            source_id=source_id,
            evidence={'description': 'Reviewed source document'},
        )[0]

    def test_queue_exposes_filterable_states_and_only_assigned_deals(self):
        suggested = self._deal('Suggested', 'FUND1')
        self._suggest(suggested)
        conflicting = self._deal('Conflicting', 'FUND2')
        self._suggest(conflicting, date(2026, 3, 11), 'one')
        self._suggest(conflicting, date(2026, 3, 12), 'two')
        self._deal('Unknown', 'FUND3')
        Deal.objects.create(title='Not assigned', fund='FUND1')

        response = self.client.get('/api/deals/receipt-date-remediation/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 3)
        self.assertEqual(response.data['counts'], {
            'SUGGESTED': 1,
            'CONFLICTING': 1,
            'REJECTED': 0,
            'UNKNOWN': 1,
        })
        filtered = self.client.get(
            '/api/deals/receipt-date-remediation/',
            {'fund': 'FUND2', 'status': 'CONFLICTING', 'search': 'conflict'},
        )
        self.assertEqual(filtered.data['count'], 1)
        self.assertEqual(filtered.data['results'][0]['id'], str(conflicting.id))

    def test_accept_sets_canonical_date_and_creates_append_only_audit(self):
        deal = self._deal()
        suggestion = self._suggest(deal)

        response = self.client.patch(
            f'/api/deals/{deal.id}/receipt-date-suggestions/{suggestion.id}/',
            {
                'decision': 'ACCEPT',
                'expected_updated_at': deal.updated_at.isoformat(),
                'note': 'Verified against the dated source.',
            },
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        deal.refresh_from_db()
        suggestion.refresh_from_db()
        self.assertEqual(deal.received_at, date(2026, 3, 10))
        self.assertEqual(suggestion.status, DealReceiptDateSuggestion.Status.ACCEPTED)
        self.assertEqual(suggestion.reviewed_by, self.user)
        audit = DealReceiptDateAudit.objects.get(deal=deal)
        self.assertEqual(audit.new_date, deal.received_at)
        self.assertEqual(audit.reviewer, self.user)
        self.assertEqual(audit.evidence, suggestion.evidence)

    def test_conflicting_accept_requires_explicit_resolution(self):
        deal = self._deal()
        first = self._suggest(deal, date(2026, 4, 1), 'source:a')
        second = self._suggest(deal, date(2026, 4, 2), 'source:b')
        payload = {
            'decision': 'ACCEPT',
            'expected_updated_at': deal.updated_at.isoformat(),
        }

        blocked = self.client.patch(
            f'/api/deals/{deal.id}/receipt-date-suggestions/{first.id}/',
            payload,
            format='json',
        )
        self.assertEqual(blocked.status_code, 409)
        deal.refresh_from_db()
        self.assertIsNone(deal.received_at)

        resolved = self.client.patch(
            f'/api/deals/{deal.id}/receipt-date-suggestions/{first.id}/',
            {**payload, 'resolve_conflict': True},
            format='json',
        )
        self.assertEqual(resolved.status_code, 200)
        second.refresh_from_db()
        self.assertEqual(second.status, DealReceiptDateSuggestion.Status.REJECTED)

    def test_reject_records_reviewer_without_mutating_canonical_date(self):
        deal = self._deal()
        suggestion = self._suggest(deal)

        response = self.client.patch(
            f'/api/deals/{deal.id}/receipt-date-suggestions/{suggestion.id}/',
            {
                'decision': 'REJECT',
                'expected_updated_at': deal.updated_at.isoformat(),
                'note': 'Source is not authoritative.',
            },
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        deal.refresh_from_db()
        suggestion.refresh_from_db()
        self.assertIsNone(deal.received_at)
        self.assertEqual(suggestion.status, DealReceiptDateSuggestion.Status.REJECTED)
        self.assertIsNotNone(suggestion.reviewed_at)
        self.assertFalse(DealReceiptDateAudit.objects.filter(deal=deal).exists())

    def test_manual_evidence_is_required_and_stale_or_unassigned_edits_fail(self):
        deal = self._deal()
        invalid = self.client.post(
            f'/api/deals/{deal.id}/receipt-date-suggestions/',
            {'proposed_date': '2026-05-03'},
            format='json',
        )
        self.assertEqual(invalid.status_code, 400)

        suggestion = self._suggest(deal)
        stale = deal.updated_at
        deal.city = 'Mumbai'
        deal.save(update_fields=['city', 'updated_at'])
        stale_response = self.client.patch(
            f'/api/deals/{deal.id}/receipt-date-suggestions/{suggestion.id}/',
            {'decision': 'REJECT', 'expected_updated_at': stale.isoformat()},
            format='json',
        )
        self.assertEqual(stale_response.status_code, 409)

        unassigned = Deal.objects.create(title='Unassigned')
        unauthorized = self.client.post(
            f'/api/deals/{unassigned.id}/receipt-date-suggestions/',
            {
                'proposed_date': '2026-05-03',
                'source_id': 'manual:1',
                'evidence': 'Dated document',
            },
            format='json',
        )
        self.assertEqual(unauthorized.status_code, 403)

    def test_receipt_date_ordering_keeps_unknown_dates_last_in_both_directions(self):
        self._deal('Unknown')
        early = self._deal('Early')
        early.received_at = date(2026, 1, 1)
        early.save(update_fields=['received_at', 'updated_at'])
        late = self._deal('Late')
        late.received_at = date(2026, 2, 1)
        late.save(update_fields=['received_at', 'updated_at'])

        ascending = self.client.get('/api/deals/', {'ordering': 'received_at'})
        descending = self.client.get('/api/deals/', {'ordering': '-received_at'})

        self.assertEqual([row['title'] for row in ascending.data['results']], ['Early', 'Late', 'Unknown'])
        self.assertEqual([row['title'] for row in descending.data['results']], ['Late', 'Early', 'Unknown'])

    def test_ledger_exposes_the_same_receipt_evidence_state_as_the_queue(self):
        deal = self._deal('Queue and ledger')
        self._suggest(deal)

        ledger = self.client.get('/api/deals/', {'search': 'Queue and ledger'})

        self.assertEqual(ledger.status_code, 200)
        self.assertEqual(ledger.data['results'][0]['receipt_date_state'], 'SUGGESTED')
        self.assertTrue(ledger.data['results'][0]['receipt_date_has_evidence'])
