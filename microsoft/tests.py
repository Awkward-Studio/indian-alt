from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from deals.models import Deal
from meetings.models import MeetingNote
from microsoft.models import Email, EmailAccount
from microsoft.services.granola_meeting_ingestion import GranolaMeetingEmailIngestionService


class GranolaMeetingEmailIngestionTests(TestCase):
    def setUp(self):
        self.account = EmailAccount.objects.create(email="dms-demo@india-alt.com")
        self.deal = Deal.objects.create(title="Acme Health")

    def _email(self, *, subject="Acme Health", body="", from_email="notes@granola.ai"):
        return Email.objects.create(
            email_account=self.account,
            graph_id=f"graph-{Email.objects.count() + 1}",
            subject=subject,
            from_email=from_email,
            body_text=body,
            date_received=timezone.now(),
        )

    @patch("ai_orchestrator.services.embedding_processor.EmbeddingService.vectorize_meeting_note", return_value=True)
    def test_processes_granola_email_when_subject_is_exact_deal_name(self, _vectorize):
        email = self._email(
            body=(
                "Date: 2026-06-20\n\n"
                "Summary:\n"
                "Management discussed growth and margins.\n\n"
                "Transcript:\n"
                "Speaker A: Revenue grew year over year.\n"
                "Speaker B: EBITDA margins improved."
            )
        )

        note = GranolaMeetingEmailIngestionService.process_email(email)

        self.assertIsNotNone(note)
        self.assertEqual(note.summary, "Management discussed growth and margins.")
        self.assertIn("Revenue grew year over year", note.body)
        self.assertEqual(list(note.deals.all()), [self.deal])
        self.assertEqual(note.source_email, email)
        email.refresh_from_db()
        self.assertEqual(email.deal, self.deal)
        self.assertTrue(email.is_processed)
        self.assertEqual(email.processing_status, "completed")

    @patch("ai_orchestrator.services.embedding_processor.EmbeddingService.vectorize_meeting_note", return_value=True)
    def test_processes_granola_email_when_deal_name_is_in_body(self, _vectorize):
        email = self._email(
            subject="Granola meeting notes",
            body=(
                "deal_name=Acme Health\n\n"
                "## Summary\n"
                "This was a customer diligence call.\n\n"
                "## Transcript\n"
                "Speaker A: Customers gave positive feedback."
            ),
        )

        note = GranolaMeetingEmailIngestionService.process_email(email)

        self.assertIsNotNone(note)
        self.assertEqual(note.summary, "This was a customer diligence call.")
        self.assertEqual(list(note.deals.all()), [self.deal])

    @patch("ai_orchestrator.services.embedding_processor.EmbeddingService.vectorize_meeting_note", return_value=True)
    def test_skips_when_deal_name_is_not_exact(self, _vectorize):
        email = self._email(
            subject="Acme Health follow-up",
            body=(
                "Summary:\n"
                "This should not be saved.\n\n"
                "Transcript:\n"
                "Speaker A: Non exact subject."
            ),
        )

        note = GranolaMeetingEmailIngestionService.process_email(email)

        self.assertIsNone(note)
        self.assertEqual(MeetingNote.objects.count(), 0)

    @patch("ai_orchestrator.services.embedding_processor.EmbeddingService.vectorize_meeting_note", return_value=True)
    def test_skips_non_granola_sender(self, _vectorize):
        email = self._email(
            from_email="banker@example.com",
            body=(
                "deal_name=Acme Health\n\n"
                "Summary:\n"
                "This should not be saved.\n\n"
                "Transcript:\n"
                "Speaker A: Non Granola sender."
            ),
        )

        note = GranolaMeetingEmailIngestionService.process_email(email)

        self.assertIsNone(note)
        self.assertEqual(MeetingNote.objects.count(), 0)
