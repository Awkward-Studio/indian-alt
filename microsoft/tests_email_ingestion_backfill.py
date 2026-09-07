import json
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from microsoft.models import Email, EmailAccount, EmailIngestionBackfill, EmailIngestionRun


class EmailBackfillTests(TestCase):
    def setUp(self):
        account = EmailAccount.objects.create(email='backfill@example.test')
        self.emails = [Email.objects.create(email_account=account, graph_id=f'backfill-{i}') for i in range(3)]

    def test_dry_run_makes_no_records_and_prints_no_private_fields(self):
        stream = StringIO()
        call_command('reconcile_email_ingestion', '--mailbox', 'backfill@example.test', '--limit', '2', '--dry-run', stdout=stream)
        payload = json.loads(stream.getvalue())
        self.assertEqual(payload['selected'], 2)
        self.assertEqual(EmailIngestionRun.objects.count(), 0)
        self.assertNotIn('subject', stream.getvalue())

    @patch('microsoft.services.email_ingestion.EmailIngestionService.dispatch')
    def test_apply_is_resumable_and_idempotent(self, dispatch):
        for _ in range(3):
            call_command('reconcile_email_ingestion', '--mailbox', 'backfill@example.test',
                         '--limit', '2', '--resume-key', 'pilot', '--apply', stdout=StringIO())
        self.assertEqual(EmailIngestionRun.objects.count(), 3)
        progress = EmailIngestionBackfill.objects.get(key='pilot')
        self.assertTrue(progress.completed)
        self.assertEqual(progress.stats['selected'], 3)

    @patch('microsoft.services.email_ingestion.EmailIngestionService.dispatch')
    def test_existing_deal_link_is_not_changed(self, dispatch):
        from deals.models import Deal
        deal = Deal.objects.create(title='Confirmed')
        self.emails[0].deal = deal
        self.emails[0].save()
        call_command('reconcile_email_ingestion', '--email-id', str(self.emails[0].id), '--apply', stdout=StringIO())
        self.emails[0].refresh_from_db()
        self.assertEqual(self.emails[0].deal, deal)
