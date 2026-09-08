import base64
import tempfile
import json
import uuid
from unittest.mock import patch

from django.test import TestCase
from deals.models import Deal, DealDocument
from meetings.models import MeetingNote
from microsoft.models import Email, EmailAccount, EmailEvidenceLink, EmailPrivateBlob
from microsoft.services.email_evidence import EmailEvidenceService as Evidence
from microsoft.services.email_matching import EmailDecisionService as Decisions
from deals.services.research_acquisition import ResearchAcquisitionService
from deals.services.document_artifacts import DocumentArtifactService


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

    @patch.object(ResearchAcquisitionService, 'download')
    def test_supported_embedded_link_becomes_a_deal_document(self, download):
        download.return_value = (
            b'linked diligence document',
            {'final_url': 'https://files.example.test/deck.pdf', 'content_type': 'application/pdf'},
        )
        self.email.body_html = '<p>Review the <a href="https://files.example.test/deck.pdf">deck.pdf</a>.</p>'
        self.email.save(update_fields=['body_html'])
        run = Evidence.snapshot(self.email)

        with tempfile.TemporaryDirectory() as tmp:
            storage = EmailPrivateBlob._meta.get_field('file').storage
            with patch.object(storage, '_location', tmp):
                storage.__dict__.pop('location', None)
                self.assertEqual(Evidence.save_links(run), [])
                self.assertEqual(Evidence.save_links(run, self.deal), [])
                storage.__dict__.pop('location', None)

        link = EmailEvidenceLink.objects.get(kind='email_link')
        self.assertEqual(link.document.deal, self.deal)
        self.assertEqual(link.document.title, 'deck.pdf')
        self.assertEqual(run.occurrences.get(source_key__startswith='link:').status, 'saved')

    def test_google_drive_share_url_is_resolved_to_file_download(self):
        resolved = Evidence._provider_download_url(
            'https://drive.google.com/file/d/file-123/view?usp=sharing'
        )
        self.assertIn('drive.usercontent.google.com/download?', resolved)
        self.assertIn('id=file-123', resolved)

    @patch.object(ResearchAcquisitionService, 'download')
    def test_link_uses_downloaded_filename_and_type(self, download):
        download.return_value = (
            b'%PDF linked diligence',
            {
                'final_url': 'https://drive.usercontent.google.com/download?id=file-123',
                'content_type': 'application/pdf',
                'filename': '3TenX Teaser.pdf',
            },
        )
        self.email.body_html = '<a href="https://drive.google.com/file/d/file-123/view">click here</a>'
        self.email.save(update_fields=['body_html'])
        run = Evidence.snapshot(self.email)

        with tempfile.TemporaryDirectory() as tmp:
            storage = EmailPrivateBlob._meta.get_field('file').storage
            with patch.object(storage, '_location', tmp):
                storage.__dict__.pop('location', None)
                self.assertEqual(Evidence.save_links(run, self.deal), [])
                storage.__dict__.pop('location', None)

        download.assert_called_once()
        self.assertIn('drive.usercontent.google.com/download?', download.call_args.args[0])
        self.assertEqual(EmailEvidenceLink.objects.get(kind='email_link').document.title, '3TenX Teaser.pdf')

    def test_document_artifact_metadata_is_json_safe(self):
        artifact = DocumentArtifactService.build_document_artifact(
            file_name='email.txt', extracted_text='', source_metadata={'source_id': uuid.uuid4()},
        )
        json.dumps(artifact)

    def test_meeting_uses_meeting_source(self):
        self.email.body_text = 'Summary\nDiscussed expansion\nTranscript\nTeam agreed to launch.'
        self.email.save()
        run = self.save_body()
        self.assertEqual(MeetingNote.objects.count(), 1)
        self.assertEqual(DealDocument.objects.count(), 1)
        self.assertEqual(MeetingNote.objects.get().deals.get(), self.deal)
        self.assertEqual(run.occurrences.get().evidence.document, DealDocument.objects.get())

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
