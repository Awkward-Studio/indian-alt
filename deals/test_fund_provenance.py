import importlib
from pathlib import Path
from tempfile import TemporaryDirectory

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from openpyxl import Workbook
from rest_framework.test import APIClient

from accounts.models import Profile
from deals.management.commands.reconcile_fund_workbooks import WORKBOOK_FIELDS
from deals.models import (
    Deal,
    DealReceiptDateSuggestion,
    FundClassificationSourceType,
    FundClassificationState,
)


class FundClassificationProvenanceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='fund-reviewer', password='password')
        self.profile = Profile.objects.create(
            user=self.user,
            name='Fund Reviewer',
            email='fund-reviewer@example.com',
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_migration_backfill_is_reversible_idempotent_and_preserves_funds(self):
        fund_one = Deal.objects.create(title='Fund One', fund='FUND1')
        fund_two = Deal.objects.create(title='Fund Two', fund='FUND2')
        fund_three = Deal.objects.create(title='Fund Three', fund='FUND3')
        before = {
            fund: Deal.objects.filter(fund=fund).count()
            for fund in ('FUND1', 'FUND2', 'FUND3')
        }
        migration = importlib.import_module(
            'deals.migrations.0040_deal_fund_classification_reviewed_at_and_more'
        )

        class Apps:
            @staticmethod
            def get_model(app_label, model_name):
                return Deal

        migration.backfill_fund_classification_provenance(Apps(), None)
        migration.backfill_fund_classification_provenance(Apps(), None)

        fund_one.refresh_from_db()
        fund_two.refresh_from_db()
        fund_three.refresh_from_db()
        self.assertEqual(fund_one.fund_classification_state, FundClassificationState.EXPLICIT)
        self.assertEqual(fund_two.fund_classification_state, FundClassificationState.EXPLICIT)
        self.assertEqual(fund_three.fund_classification_state, FundClassificationState.DEFAULTED)
        self.assertEqual(before, {
            fund: Deal.objects.filter(fund=fund).count()
            for fund in ('FUND1', 'FUND2', 'FUND3')
        })

        migration.reverse_fund_classification_provenance(Apps(), None)
        self.assertFalse(Deal.objects.exclude(
            fund_classification_state=FundClassificationState.DEFAULTED
        ).exists())

    def test_exact_workbook_match_records_row_provenance(self):
        deal = Deal.objects.create(title='Evidence Co', fund='FUND3')
        with TemporaryDirectory() as directory:
            headers = sorted(WORKBOOK_FIELDS)
            workbook = Workbook()
            sheet = workbook.active
            sheet.append(headers)
            values = {header: '' for header in headers}
            values.update({
                'Deal Name': 'Evidence Co',
                'Fund': 'FUND3',
                'Date of Receipt': '2026-07-09',
            })
            sheet.append([values[header] for header in headers])
            workbook.save(Path(directory) / '3. Fund III.xlsx')

            call_command(
                'reconcile_fund_workbooks',
                source_dir=directory,
                fund=['FUND3'],
                apply=True,
            )

        deal.refresh_from_db()
        self.assertEqual(deal.fund, 'FUND3')
        self.assertEqual(deal.fund_classification_state, FundClassificationState.EXPLICIT)
        self.assertEqual(deal.fund_classification_source_type, FundClassificationSourceType.WORKBOOK)
        self.assertEqual(deal.fund_classification_source_id, '3. Fund III.xlsx:row:2')
        self.assertIsNone(deal.received_at)
        suggestion = DealReceiptDateSuggestion.objects.get(deal=deal)
        self.assertEqual(suggestion.proposed_date.isoformat(), '2026-07-09')
        self.assertEqual(suggestion.source_type, DealReceiptDateSuggestion.SourceType.WORKBOOK)
        self.assertEqual(suggestion.evidence['workbook'], '3. Fund III.xlsx')

    def test_state_filter_and_summary_are_queryable(self):
        explicit = Deal.objects.create(
            title='Explicit',
            fund='FUND1',
            fund_classification_state=FundClassificationState.EXPLICIT,
            fund_classification_source_type=FundClassificationSourceType.LEGACY_IMPORT,
            fund_classification_source_id='fixture:FUND1',
        )
        defaulted = Deal.objects.create(title='Defaulted', fund='FUND3')
        uncertain = Deal.objects.create(
            title='Uncertain',
            fund='FUND3',
            fund_classification_state=FundClassificationState.UNCERTAIN,
        )
        for deal in (explicit, defaulted, uncertain):
            deal.responsibility.add(self.profile)

        response = self.client.get('/api/deals/', {'fund_classification_state': 'UNCERTAIN'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row['id'] for row in response.data['results']], [str(uncertain.id)])

        summary = self.client.get('/api/deals/fund-classification-summary/')
        self.assertEqual(summary.status_code, 200)
        self.assertEqual(summary.data['counts'], {
            'EXPLICIT': 1,
            'DEFAULTED': 1,
            'UNCERTAIN': 1,
            'total': 3,
        })

    def test_authorized_review_records_actor_time_and_supports_reclassification(self):
        deal = Deal.objects.create(
            title='Review me',
            fund='FUND3',
            fund_classification_state=FundClassificationState.UNCERTAIN,
        )
        deal.responsibility.add(self.profile)

        response = self.client.patch(
            f'/api/deals/{deal.id}/fund-classification/',
            {
                'fund': 'FUND2',
                'source_id': 'investment-committee-review-2026-08-13',
                'expected_updated_at': deal.updated_at.isoformat(),
            },
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        deal.refresh_from_db()
        self.assertEqual(deal.fund, 'FUND2')
        self.assertEqual(deal.fund_classification_state, FundClassificationState.EXPLICIT)
        self.assertEqual(deal.fund_classification_source_type, FundClassificationSourceType.ANALYST_REVIEW)
        self.assertEqual(deal.fund_classification_reviewed_by, self.user)
        self.assertIsNotNone(deal.fund_classification_reviewed_at)

    def test_stale_or_unauthorized_review_cannot_change_classification(self):
        deal = Deal.objects.create(
            title='Protected',
            fund='FUND3',
            fund_classification_state=FundClassificationState.UNCERTAIN,
        )
        unauthorized = self.client.patch(
            f'/api/deals/{deal.id}/fund-classification/',
            {'fund': 'FUND1', 'expected_updated_at': deal.updated_at.isoformat()},
            format='json',
        )
        self.assertEqual(unauthorized.status_code, 403)

        deal.responsibility.add(self.profile)
        stale = deal.updated_at
        deal.city = 'Mumbai'
        deal.save(update_fields=['city', 'updated_at'])
        response = self.client.patch(
            f'/api/deals/{deal.id}/fund-classification/',
            {'fund': 'FUND1', 'expected_updated_at': stale.isoformat()},
            format='json',
        )
        self.assertEqual(response.status_code, 409)
        deal.refresh_from_db()
        self.assertEqual(deal.fund, 'FUND3')
        self.assertEqual(deal.fund_classification_state, FundClassificationState.UNCERTAIN)
