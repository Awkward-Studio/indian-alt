from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from ai_orchestrator.models import DocumentChunk
from deals.models import Deal, DealDocument
from deals.services.folder_analysis import FolderAnalysisService
from microsoft.models import (
    Email,
    EmailAccount,
    EmailContributionOccurrence,
    EmailEvidenceLink,
    EmailIngestionRun,
)


class DealDocumentVDRLifecycleTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="vdr-user", password="test-only")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.deal = Deal.objects.create(title="VDR company")

    def test_delete_removes_chunks_and_keeps_email_source_as_a_visible_gap(self):
        account = EmailAccount.objects.create(email="vdr@example.test")
        email = Email.objects.create(
            email_account=account,
            graph_id="vdr-email",
            subject="Linked deal source",
            body_text="Deal evidence",
            deal=self.deal,
        )
        run = EmailIngestionRun.objects.create(
            email=email,
            input_version="a" * 64,
            status="completed",
            match={"status": "matched", "deal_id": str(self.deal.id)},
        )
        document = DealDocument.objects.create(deal=self.deal, title="Email body")
        link = EmailEvidenceLink.objects.create(
            email_account=account,
            deal=self.deal,
            source_key="body:source",
            kind="email_body",
            document=document,
            index_status="completed",
        )
        occurrence = EmailContributionOccurrence.objects.create(
            run=run,
            source_key="body:0",
            evidence=link,
            status="saved",
        )
        chunk = DocumentChunk.objects.create(
            deal=self.deal,
            source_type="document",
            source_id=str(document.id),
            content="indexed evidence",
        )

        response = self.client.delete(f"/api/deals/documents/{document.id}/")

        self.assertEqual(response.status_code, 204)
        self.assertFalse(DealDocument.objects.filter(pk=document.id).exists())
        self.assertFalse(DocumentChunk.objects.filter(pk=chunk.id).exists())
        link.refresh_from_db()
        occurrence.refresh_from_db()
        self.assertIsNone(link.document_id)
        self.assertEqual(link.index_status, "pending")
        self.assertIn("deleted", link.error.lower())
        self.assertEqual(occurrence.status, "pending")

    @patch("deals.services.document_artifacts.DocumentArtifactService.artifact_complete", return_value=True)
    def test_failed_email_link_is_a_report_gap_without_hiding_ready_documents(self, _complete):
        account = EmailAccount.objects.create(email="gap@example.test")
        email = Email.objects.create(
            email_account=account,
            graph_id="gap-email",
            subject="Email with linked deck",
            body_text="Email evidence",
            deal=self.deal,
        )
        run = EmailIngestionRun.objects.create(
            email=email,
            input_version="b" * 64,
            status="completed",
            match={"status": "matched", "deal_id": str(self.deal.id)},
        )
        DealDocument.objects.create(deal=self.deal, title="Email body", is_indexed=True)
        EmailContributionOccurrence.objects.create(
            run=run,
            source_key="link:missing",
            status="failed",
            error="Linked document could not be downloaded.",
            metadata={"name": "Pitch deck.pdf", "url": "https://files.example.test/deck.pdf"},
        )

        readiness = FolderAnalysisService.deal_analysis_readiness(self.deal)

        self.assertFalse(readiness["ready"])
        self.assertTrue(readiness["can_build_with_gaps"])
        self.assertEqual(readiness["ready_count"], 1)
        self.assertEqual(readiness["gap_count"], 1)
        self.assertEqual(readiness["source_gaps"][0]["title"], "Pitch deck.pdf")
        self.assertEqual(readiness["source_gaps"][0]["source_kind"], "email_link")
