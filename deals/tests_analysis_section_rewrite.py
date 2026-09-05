from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient
from unittest.mock import MagicMock, patch

from ai_orchestrator.models import DocumentChunk
from deals.models import AnalysisKind, Deal, DealAnalysis, DealDocument
from deals.services.analysis_section_rewrite import AnalysisSectionRewriteService
from meetings.models import MeetingNote


REPORT = """# Investment Committee Note

## Company Details

Legacy company description.

## Key Financials

FY25 revenue was INR 90 Cr and EBITDA margin was 8%.

## Risk Factors

Customer concentration requires diligence.
"""


class AnalysisSectionReplacementTests(SimpleTestCase):
    def test_replaces_only_requested_markdown_section(self):
        updated = AnalysisSectionRewriteService.replace_section(
            REPORT,
            "Key Financials",
            "## Key Financials\n\nFY26 revenue was INR 128 Cr and EBITDA margin was 14%.",
        )

        self.assertIn("FY26 revenue was INR 128 Cr", updated)
        self.assertNotIn("FY25 revenue was INR 90 Cr", updated)
        self.assertIn("Legacy company description", updated)
        self.assertIn("Customer concentration requires diligence", updated)

    def test_missing_section_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "was not found"):
            AnalysisSectionRewriteService.replace_section(REPORT, "Exit Considerations", "New text")


class PersistedAnalysisSectionRewriteTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="rewrite-admin", password="test", is_staff=True)
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.deal = Deal.objects.create(title="Project Monsoon", deal_summary=REPORT)
        self.analysis = DealAnalysis.objects.create(
            deal=self.deal,
            version=1,
            analysis_kind=AnalysisKind.INITIAL,
            analysis_json={
                "analyst_report": REPORT,
                "canonical_snapshot": {"analyst_report": REPORT},
            },
        )

    @patch("ai_orchestrator.services.embedding_processor.EmbeddingService.search_global_chunks")
    def test_rewrite_prompt_retrieves_indexed_meeting_evidence(self, search_chunks):
        note = MeetingNote.objects.create(
            title="FY26 management review",
            body="FY26 revenue reached INR 128 Cr and EBITDA margin reached 14%.",
            summary="Audited FY26 financial review.",
            is_indexed=True,
            chunk_count=1,
        )
        note.deals.add(self.deal)
        chunk = DocumentChunk.objects.create(
            deal=self.deal,
            source_type="meeting_note",
            source_id=str(note.id),
            content=note.body,
            search_text=note.body,
            embedding=[0.0] * 1024,
            embedding_model="test-embedding",
            embedding_dimensions=1024,
            metadata={"title": note.title, "meeting_note_id": str(note.id)},
        )
        search_chunks.return_value = [chunk]
        ai_service = MagicMock()
        ai_service.process_content.return_value = {"response": "## Key Financials\n\nUpdated."}

        AnalysisSectionRewriteService(ai_service).rewrite(
            deal=self.deal,
            section_title="Key Financials",
            section_markdown=AnalysisSectionRewriteService.locate_section(REPORT, "Key Financials")[0],
            instruction="Use the FY26 management meeting.",
            full_report=REPORT,
            version=1,
        )

        prompt = ai_service.process_content.call_args.kwargs["content"]
        self.assertIn("RELEVANT INDEXED MEETING EVIDENCE", prompt)
        self.assertIn("FY26 management review", prompt)
        self.assertIn("FY26 revenue reached INR 128 Cr", prompt)
        search_chunks.assert_called_once()

    @patch("ai_orchestrator.services.embedding_processor.EmbeddingService.search_global_chunks")
    def test_rewrite_prompt_retrieves_indexed_company_news_evidence(self, search_chunks):
        document = DealDocument.objects.create(
            deal=self.deal,
            title="Public Domain News Research - 2026-08-06",
            extracted_text="Perfora reported rapid revenue growth in FY24.",
            normalized_text="Perfora reported rapid revenue growth in FY24.",
            is_indexed=True,
        )
        chunk = DocumentChunk.objects.create(
            deal=self.deal,
            source_type="document",
            source_id=str(document.id),
            content="Perfora revenue increased from INR 1.4 Cr in FY22 to INR 42 Cr in FY24.",
            search_text="Perfora revenue growth FY22 FY24",
            embedding=[0.0] * 1024,
            embedding_model="test-embedding",
            embedding_dimensions=1024,
            metadata={"title": document.title},
        )
        search_chunks.return_value = [chunk]
        ai_service = MagicMock()
        ai_service.process_content.return_value = {"response": "## Key Financials\n\nUpdated."}

        AnalysisSectionRewriteService(ai_service).rewrite(
            deal=self.deal,
            section_title="Key Financials",
            section_markdown=AnalysisSectionRewriteService.locate_section(REPORT, "Key Financials")[0],
            instruction="Use the latest public news evidence.",
            full_report=REPORT,
            version=1,
        )

        prompt = ai_service.process_content.call_args.kwargs["content"]
        self.assertIn("RELEVANT INDEXED COMPANY NEWS EVIDENCE", prompt)
        self.assertIn(document.title, prompt)
        self.assertIn("INR 42 Cr in FY24", prompt)
        search_chunks.assert_called_once()

    @patch("deals.services.analysis_section_rewrite.AIProcessorService.process_content")
    def test_api_requires_confirmation_then_persists_rewrite(self, process_content):
        process_content.return_value = {
            "response": (
                "## Key Financials\n\n"
                "Management confirmed FY26 revenue of INR 128 Cr, a 42% increase, "
                "and EBITDA margin improved to 14% after pricing actions."
            )
        }

        payload = {
            "section_title": "Key Financials",
            "section_markdown": AnalysisSectionRewriteService.locate_section(
                REPORT, "Key Financials"
            )[0],
            "instruction": (
                "Use the analyzed meetings: replace the old FY25 figures with FY26 revenue "
                "of INR 128 Cr, 42% growth, and 14% EBITDA margin."
            ),
            "full_report": REPORT,
            "version": 1,
        }
        preview = self.client.post(
            f"/api/deals/{self.deal.id}/rewrite_analysis_section/",
            payload,
            format="json",
        )

        self.assertEqual(preview.status_code, 200)
        self.assertFalse(preview.data["persisted"])
        self.assertTrue(preview.data["confirmation_required"])
        self.analysis.refresh_from_db()
        self.assertEqual(self.analysis.analysis_json["analyst_report"], REPORT)

        confirmed = self.client.post(
            f"/api/deals/{self.deal.id}/rewrite_analysis_section/",
            {**payload, "confirmation_token": preview.data["confirmation_token"]},
            format="json",
        )

        self.assertEqual(confirmed.status_code, 200)
        self.assertTrue(confirmed.data["persisted"])
        self.analysis.refresh_from_db()
        self.deal.refresh_from_db()
        saved = self.analysis.analysis_json["analyst_report"]
        self.assertIn("FY26 revenue of INR 128 Cr", saved)
        self.assertNotIn("FY25 revenue was INR 90 Cr", saved)
        self.assertEqual(self.deal.deal_summary, saved)
        self.assertEqual(self.analysis.analysis_json["canonical_snapshot"]["analyst_report"], saved)
        process_content.assert_called_once()

    @patch("deals.services.analysis_section_rewrite.AIProcessorService.process_content")
    def test_confirmation_rejects_changed_report(self, process_content):
        process_content.return_value = {"response": "## Key Financials\n\nUpdated facts."}
        payload = {
            "section_title": "Key Financials",
            "section_markdown": AnalysisSectionRewriteService.locate_section(REPORT, "Key Financials")[0],
            "instruction": "Update from meetings.",
            "full_report": REPORT,
            "version": 1,
        }
        preview = self.client.post(
            f"/api/deals/{self.deal.id}/rewrite_analysis_section/", payload, format="json"
        )
        response = self.client.post(
            f"/api/deals/{self.deal.id}/rewrite_analysis_section/",
            {
                **payload,
                "full_report": REPORT + "\nChanged elsewhere.",
                "confirmation_token": preview.data["confirmation_token"],
            },
            format="json",
        )

        self.assertEqual(response.status_code, 409)
        self.analysis.refresh_from_db()
        self.assertEqual(self.analysis.analysis_json["analyst_report"], REPORT)

    def test_rewrite_prompt_retrieves_attached_deal_document_evidence(self):
        document = DealDocument.objects.create(
            deal=self.deal,
            title="Q3_Investor_Deck.pdf",
            document_type="Pitch Deck",
            extracted_text="Unit economics improved to 28% gross margin in Q3.",
            normalized_text="Unit economics improved to 28% gross margin in Q3.",
            is_indexed=False,
        )
        ai_service = MagicMock()
        ai_service.process_content.return_value = {"response": "## Key Financials\n\nUpdated with deck facts."}

        AnalysisSectionRewriteService(ai_service).rewrite(
            deal=self.deal,
            section_title="Key Financials",
            section_markdown=AnalysisSectionRewriteService.locate_section(REPORT, "Key Financials")[0],
            instruction="Include the unit economics from the attached deck.",
            full_report=REPORT,
            version=1,
            document_ids=[str(document.id)],
        )

        prompt = ai_service.process_content.call_args.kwargs["content"]
        self.assertIn("ATTACHED DEAL DOCUMENTS CONTEXT", prompt)
        self.assertIn("Q3_Investor_Deck.pdf", prompt)
        self.assertIn("28% gross margin in Q3", prompt)
