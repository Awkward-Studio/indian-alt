import base64
import tempfile
from unittest.mock import patch

from django.test import TestCase
from deals.models import Deal, DealDocument
from meetings.models import MeetingNote
from microsoft.models import Email, EmailAccount, EmailEvidenceLink, EmailPrivateBlob
from microsoft.services.email_evidence import EmailEvidenceService as Evidence
from microsoft.services.email_matching import EmailDecisionService as Decisions


class EmailEvidenceTests(TestCase):
    def setUp(self):
        self.account = EmailAccount.objects.create(email='evidence@example.test')
        self.deal = Deal.objects.create(title='Evidence company')
        self.email = Email.objects.create(email_account=self.account, graph_id='evidence', subject='Update', body_text='Revised amount is 140 crore.')

    def save_body(self, email=None):
        run = Evidence.snapshot(email or self.email)
        parts = Evidence.parts(run, self.deal)
        Evidence.save_parts(run, self.deal, parts, Decisions.classify(parts, source_id=run.email_id, use_ai=False))
        return run

    def test_normal_body_is_document_and_retry_reuses_it(self):
        self.save_body()
        self.save_body()
        self.assertEqual(DealDocument.objects.count(), 1)
        self.assertIn('140 crore', DealDocument.objects.get().normalized_text)
        self.email.refresh_from_db()
        self.assertEqual(self.email.deal, self.deal)

    def test_html_anchor_destination_is_saved_as_deal_evidence(self):
        self.email.body_html = '<p>Review the <a href="https://drive.example.test/deck/123">deck</a>.</p>'
        self.email.save(update_fields=['body_html'])

        self.save_body()

        self.assertIn(
            'deck <https://drive.example.test/deck/123>',
            DealDocument.objects.get().normalized_text,
        )

    def test_meeting_uses_meeting_source(self):
        self.email.body_text = 'Summary\nDiscussed expansion\nTranscript\nTeam agreed to launch.'
        self.email.save()
        self.save_body()
        self.assertEqual(MeetingNote.objects.count(), 1)
        self.assertEqual(DealDocument.objects.count(), 0)
        self.assertEqual(MeetingNote.objects.get().deals.get(), self.deal)

    @patch('microsoft.services.graph_service.GraphAPIService.get_attachment_content')
    def test_repeated_attachment_content_reuses_private_blob_and_document(self, download):
        download.return_value = {'contentBytes': base64.b64encode(b'private financial schedule').decode()}
        self.email.attachments = [{'id': 'a', 'name': 'schedule.txt'}, {'id': 'b', 'name': 'renamed.txt'}]
        self.email.save()
        run = Evidence.snapshot(self.email)
        with tempfile.TemporaryDirectory() as tmp:
            storage = EmailPrivateBlob._meta.get_field('file').storage
            with patch.object(storage, '_location', tmp):
                storage.__dict__.pop('location', None)
                self.assertEqual(Evidence.save_attachments(run, self.deal), [])
                self.assertEqual(EmailPrivateBlob.objects.count(), 1)
                self.assertEqual(DealDocument.objects.count(), 1)
                with EmailPrivateBlob.objects.get().file.open('rb') as stream:
                    self.assertEqual(stream.read(), b'private financial schedule')
                storage.__dict__.pop('location', None)

    @patch('microsoft.services.graph_service.GraphAPIService.get_attachment_content', side_effect=ValueError('unavailable'))
    def test_failed_attachment_does_not_rollback_body(self, download):
        self.email.attachments = [{'id': 'a', 'name': 'missing.pdf'}]
        self.email.save()
        run = self.save_body()
        self.assertEqual(len(Evidence.save_attachments(run, self.deal)), 1)
        self.assertEqual(DealDocument.objects.count(), 1)
        self.assertEqual(run.occurrences.filter(status='failed').count(), 1)

    def test_cross_conversation_forward_reuses_committed_long_body(self):
        self.email.body_text = 'Revenue grew and operating margins improved during the fiscal year. ' * 3
        self.email.save()
        self.save_body()
        forward = Email.objects.create(email_account=self.account, graph_id='forward', conversation_id='new',
            body_text='New reply\n' + self.email.body_text.strip() + '\nNew inline tail')
        self.save_body(forward)
        self.assertEqual(DealDocument.objects.count(), 3)
        self.assertEqual(EmailEvidenceLink.objects.count(), 3)
