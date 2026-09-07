from django.db import IntegrityError, transaction
from django.test import TestCase

from deals.models import Deal, DealDocument
from microsoft.models import Email, EmailAccount, EmailContribution, EmailEvidenceLink, EmailIngestionRun


class IngestionStateTests(TestCase):
    def setUp(self):
        self.account = EmailAccount.objects.create(email='ingestion@example.test')
        self.email = Email.objects.create(email_account=self.account, graph_id='state-test')

    def test_run_identity_and_independent_stage_states(self):
        run = EmailIngestionRun.objects.create(email=self.email, input_version='a' * 64,
            stages={'save': 'completed', 'index': 'waiting_service'})
        with self.assertRaises(IntegrityError), transaction.atomic():
            EmailIngestionRun.objects.create(email=self.email, input_version='a' * 64)
        run.refresh_from_db()
        self.assertEqual(run.stages['save'], 'completed')
        self.assertEqual(run.stages['index'], 'waiting_service')

    def test_source_identity_is_mailbox_scoped(self):
        other = EmailAccount.objects.create(email='other@example.test')
        for account in [self.account, other]:
            EmailContribution.objects.create(email_account=account, fingerprint='b' * 64, text='Yes')
        self.assertEqual(EmailContribution.objects.count(), 2)

    def test_output_identity_does_not_modify_legacy_documents(self):
        deal = Deal.objects.create(title='Example')
        legacy = DealDocument.objects.create(deal=deal, title='Legacy', onedrive_id='drive-id')
        EmailEvidenceLink.objects.create(email_account=self.account, deal=deal, source_key='body:1', kind='email_body')
        with self.assertRaises(IntegrityError), transaction.atomic():
            EmailEvidenceLink.objects.create(email_account=self.account, deal=deal, source_key='body:1', kind='email_body')
        legacy.refresh_from_db()
        self.assertEqual(legacy.onedrive_id, 'drive-id')
