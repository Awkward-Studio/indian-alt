import json
from unittest.mock import Mock, patch

from django.contrib.auth.models import User
from django.test import TestCase

from ai_orchestrator.prompt_contracts import PHASE2_ARTIFACT_REQUIRED_KEYS
from ai_orchestrator.models import DocumentChunk
from deals.models import Deal, DealDocument, DocumentType
from deals.services.manual_document import ManualDocumentEvidenceService
from deals.tasks import process_manual_document


class ManualDocumentEvidenceServiceTests(TestCase):
    def setUp(self):
        self.mock_provider = Mock()
        self.service = ManualDocumentEvidenceService(provider=self.mock_provider)

    @patch("deals.services.manual_document.AIRuntimeService.get_default_personality", return_value=None)
    @patch("deals.services.manual_document.AIRuntimeService.get_text_model", return_value="gemma-4-12b-it-q8")
    def test_build_extracts_bulk2_like_phase2_artifacts(self, mock_model, mock_personality):
        sample_response = {
            "document_type_suggestion": {
                "label": "pitch_deck",
                "display_label": "Pitch Deck",
                "confidence": "High",
                "rationale": "Contains investor overview and market sizing",
            },
            "document_summary": "Company overview and Q3 metrics.",
            "claims": ["Revenue grew 40% YoY."],
            "metrics": [{"name": "ARR", "value": "15M", "period": "FY26", "unit": "USD", "confidence": "High"}],
            "numeric_evidence": [{"line_item": "Revenue", "value": "15M", "period": "FY26", "unit": "USD", "confidence": "High", "notes": ""}],
            "table_definitions": [{"title": "Financials", "sheet_name": "P&L", "range": "A1:D10", "detected_header_rows": ["Year"], "period_columns": ["FY25", "FY26"], "metric_rows": ["Revenue"], "units": "USD", "key_highlights": ["Growth"], "source_location": "P&L!A1:D10"}],
            "risks": ["Competition in tier 1 cities"],
            "open_questions": ["Customer churn rate in Q4"],
            "diligence_gaps": ["Detailed customer concentration"],
            "citations": ["Slide 4"],
            "industry_overview": ["Market expanding rapidly"],
        }
        self.mock_provider.execute_standard.return_value = {
            "response": json.dumps(sample_response)
        }

        text = "Sample company pitch deck text content covering revenue and projections."
        artifact = self.service.build(text, "Deck.pdf")

        for key in PHASE2_ARTIFACT_REQUIRED_KEYS:
            self.assertIn(key, artifact)
        self.assertEqual(artifact["document_name"], "Deck.pdf")
        self.assertEqual(artifact["document_type"], "Pitch Deck")
        self.assertEqual(artifact["document_summary"], "[Segment 1] Company overview and Q3 metrics.")
        self.assertEqual(len(artifact["metrics"]), 1)
        self.assertEqual(artifact["metrics"][0]["name"], "ARR")
        self.assertEqual(artifact["upload_processing_status"], "complete")
        self.assertIn("document_name", artifact["source_map"])
        self.assertEqual(artifact["source_map"]["document_name"], "Deck.pdf")
        self.assertEqual(artifact["intel_coverage"]["segments_completed"], 1)

    @patch("deals.services.manual_document.AIRuntimeService.get_default_personality", return_value=None)
    @patch("deals.services.manual_document.AIRuntimeService.get_text_model", return_value="gemma-4-12b-it-q8")
    def test_build_marks_partial_on_inference_failure(self, mock_model, mock_personality):
        self.mock_provider.execute_standard.side_effect = RuntimeError("Inference timeout")
        artifact = self.service.build("Some source text", "Report.pdf")

        self.assertEqual(artifact["upload_processing_status"], "partial")
        self.assertEqual(artifact["intel_coverage"]["segments_completed"], 0)
        self.assertEqual(artifact["intel_coverage"]["failed_segments"], [1])
        self.assertIn("incomplete_gemma_evidence; full source text retained", artifact["quality_flags"])


class ProcessManualDocumentTaskTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="secretpassword")
        self.deal = Deal.objects.create(title="Test Deal Alpha")
        self.doc = DealDocument.objects.create(
            deal=self.deal,
            title="Overview.docx",
            document_type=DocumentType.OTHER,
            extracted_text="Native extracted text from Word document. Revenue is 50 crore.",
            normalized_text="Native extracted text from Word document. Revenue is 50 crore.",
            evidence_json={"upload_processing_status": "queued"},
            chunking_status="not_chunked",
        )

    @patch("deals.services.manual_document.ManualDocumentEvidenceService.build")
    def test_process_manual_document_updates_artifacts_chunks_and_deal_text(self, mock_build):
        mock_build.return_value = {
            "document_name": "Overview.docx",
            "document_type": "Pitch Deck",
            "document_type_suggestion": {"display_label": "Pitch Deck"},
            "document_summary": "Summary of overview doc.",
            "claims": ["High growth."],
            "metrics": [{"name": "Revenue", "value": "50 Cr"}],
            "numeric_evidence": [],
            "table_definitions": [{"title": "P&L"}],
            "risks": [],
            "open_questions": [],
            "diligence_gaps": [],
            "citations": [],
            "industry_overview": [],
            "source_map": {"document_name": "Overview.docx", "segments": []},
            "upload_processing_status": "complete",
        }

        result = process_manual_document(str(self.doc.id))

        self.assertEqual(result["status"], "complete")
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.document_type, DocumentType.PITCH_DECK)
        self.assertTrue(self.doc.is_indexed)
        self.assertEqual(self.doc.chunking_status, "chunked")
        self.assertTrue(self.doc.is_ai_analyzed)
        self.assertEqual(self.doc.evidence_json["document_summary"], "Summary of overview doc.")
        self.assertEqual(len(self.doc.key_metrics_json), 1)

        # Document chunks created
        chunks = DocumentChunk.objects.filter(deal=self.deal, source_id=str(self.doc.id))
        self.assertGreater(chunks.count(), 0)

        # Deal extracted text synced
        self.deal.refresh_from_db()
        self.assertIn("--- DOCUMENT: Overview.docx ---", self.deal.extracted_text)
        self.assertIn("Revenue is 50 crore.", self.deal.extracted_text)
