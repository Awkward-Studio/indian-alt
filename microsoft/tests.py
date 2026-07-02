from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

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
    def test_repeat_ingest_keeps_existing_meeting_note_edits(self, vectorize):
        email = self._email(
            subject="Granola meeting notes",
            body=(
                "deal_name=Acme Health\n\n"
                "Summary:\n"
                "Original summary from email.\n\n"
                "Transcript:\n"
                "Original transcript from email."
            ),
        )
        note = GranolaMeetingEmailIngestionService.process_email(email)
        self.assertIsNotNone(note)

        note.title = "Edited title"
        note.summary = "Edited summary"
        note.body = "Edited meeting note body"
        note.save(update_fields=["title", "summary", "body", "updated_at"])

        email.body_text = (
            "deal_name=Acme Health\n\n"
            "Summary:\n"
            "Changed summary from repeated fetch.\n\n"
            "Transcript:\n"
            "Changed transcript from repeated fetch."
        )
        email.save(update_fields=["body_text", "updated_at"])

        repeated_note = GranolaMeetingEmailIngestionService.process_email(email)
        repeated_note.refresh_from_db()

        self.assertEqual(repeated_note.id, note.id)
        self.assertEqual(repeated_note.title, "Edited title")
        self.assertEqual(repeated_note.summary, "Edited summary")
        self.assertEqual(repeated_note.body, "Edited meeting note body")
        self.assertEqual(vectorize.call_count, 1)

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
    def test_processes_non_granola_sender_when_payload_matches(self, _vectorize):
        email = self._email(
            from_email="banker@example.com",
            body=(
                "deal_name=Acme Health\n\n"
                "Summary:\n"
                "This should be saved even though the sender is not Granola.\n\n"
                "Transcript:\n"
                "Speaker A: Non Granola sender."
            ),
        )

        note = GranolaMeetingEmailIngestionService.process_email(email)

        self.assertIsNotNone(note)
        self.assertEqual(note.summary, "This should be saved even though the sender is not Granola.")
        self.assertEqual(list(note.deals.all()), [self.deal])

    def test_recognizes_meeting_note_email_without_resolved_deal(self):
        email = self._email(
            subject="Granola meeting notes",
            body=(
                "Summary:\n"
                "Discussion covered customer demand.\n\n"
                "Notes:\n"
                "The team reviewed expansion plans."
            ),
        )

        self.assertTrue(GranolaMeetingEmailIngestionService.is_meeting_note_email(email))

    def test_does_not_recognize_regular_email_as_meeting_note(self):
        email = self._email(
            subject="Acme Health follow-up",
            body="Please see the attached update from the company.",
            from_email="banker@example.com",
        )

        self.assertFalse(GranolaMeetingEmailIngestionService.is_meeting_note_email(email))


class EmailAttachDealAPITests(TestCase):
    def setUp(self):
        self.account = EmailAccount.objects.create(email="dms-demo@india-alt.com")
        self.deal = Deal.objects.create(title="Acme Health")
        self.email = Email.objects.create(
            email_account=self.account,
            graph_id="graph-attach-1",
            subject="Acme follow-up",
            from_email="banker@example.com",
            body_text="Notes from the Acme discussion.",
            date_received=timezone.now(),
        )
        user = get_user_model().objects.create_user(username="analyst@example.com", password="testpass")
        self.client = APIClient()
        self.client.force_authenticate(user=user)

    @patch("ai_orchestrator.services.embedding_processor.EmbeddingService.vectorize_meeting_note", return_value=True)
    def test_attach_deal_creates_embedded_meeting_note_without_linking_email(self, vectorize_meeting_note):
        response = self.client.post(
            f"/api/microsoft/emails/emails/{self.email.id}/attach_deal/",
            {"deal_id": str(self.deal.id)},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.email.refresh_from_db()
        self.assertIsNone(self.email.deal)
        self.assertFalse(self.email.is_processed)
        self.assertEqual(self.email.processing_status, "idle")
        self.assertIsNone(self.email.extracted_text)
        self.assertFalse(self.email.is_indexed)
        vectorize_meeting_note.assert_called_once()
        note = MeetingNote.objects.get(source_email=self.email)
        self.assertEqual(note.body, "Notes from the Acme discussion.")
        self.assertEqual(list(note.deals.all()), [self.deal])
        self.assertIsNone(response.data["deal_id"])
        self.assertIsNone(response.data.get("deal_title"))

    @patch("ai_orchestrator.services.embedding_processor.EmbeddingService.vectorize_meeting_note", return_value=False)
    def test_attach_deal_does_not_link_email_when_meeting_note_embedding_fails(self, _vectorize_meeting_note):
        response = self.client.post(
            f"/api/microsoft/emails/emails/{self.email.id}/attach_deal/",
            {"deal_id": str(self.deal.id)},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.email.refresh_from_db()
        self.assertIsNone(self.email.deal)
        self.assertFalse(self.email.is_processed)
        self.assertFalse(self.email.is_indexed)
        self.assertEqual(MeetingNote.objects.filter(source_email=self.email).count(), 0)

    def test_email_list_exposes_meeting_note_flag(self):
        meeting_email = Email.objects.create(
            email_account=self.account,
            graph_id="graph-meeting-list",
            subject="Granola meeting notes",
            from_email="notes@granola.ai",
            body_text=(
                "Summary:\n"
                "Management discussed growth.\n\n"
                "Transcript:\n"
                "Speaker A: Revenue grew."
            ),
            date_received=timezone.now(),
        )

        response = self.client.get("/api/microsoft/emails/emails/")

        self.assertEqual(response.status_code, 200)
        rows = response.data["results"] if isinstance(response.data, dict) and "results" in response.data else response.data
        flags = {str(row["id"]): row["is_meeting_note_email"] for row in rows}
        self.assertTrue(flags[str(meeting_email.id)])
        self.assertFalse(flags[str(self.email.id)])
