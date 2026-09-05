import io
from unittest.mock import Mock, patch

import fitz
from django.test import SimpleTestCase
from openpyxl import Workbook

from ai_orchestrator.services.document_processor import DocumentProcessorService


class ChatExtractionTests(SimpleTestCase):
    def setUp(self):
        self.service = DocumentProcessorService()
        self.service.provider = Mock()

    @patch.object(DocumentProcessorService, "_remote_extract", side_effect=AssertionError("docproc called"))
    def test_text_upload_needs_neither_docproc_nor_inference(self, remote):
        result = self.service.get_chat_extraction_result(b"Revenue: 0", "note.txt")
        self.assertEqual(result["text"], "Revenue: 0")
        self.service.provider.execute_standard.assert_not_called()

    def test_native_pdf_is_read_without_vision(self):
        with fitz.open() as doc:
            doc.new_page().insert_text((72, 72), "Acme revenue 123")
            result = self.service.get_chat_extraction_result(doc.tobytes(), "report.pdf")
        self.assertIn("Acme revenue 123", result["text"])
        self.assertIn("PAGE 1", result["text"])
        self.service.provider.execute_standard.assert_not_called()

    @patch("ai_orchestrator.services.document_processor.AIRuntimeService.get_default_personality", return_value=None)
    @patch("ai_orchestrator.services.document_processor.AIRuntimeService.get_text_model", return_value="gemma-test")
    def test_mixed_pdf_uses_vision_only_for_scanned_page(self, model, personality):
        self.service.provider.execute_standard.return_value = {"response": "Scanned evidence"}
        with fitz.open() as doc:
            doc.new_page().insert_text((72, 72), "Native evidence")
            doc.new_page()
            result = self.service.get_chat_extraction_result(doc.tobytes(), "mixed.pdf")
        self.assertIn("Native evidence", result["text"])
        self.assertIn("Scanned evidence", result["text"])
        self.service.provider.execute_standard.assert_called_once()
        self.assertEqual(result["render_metadata"]["vision_pages"], 1)

    @patch("ai_orchestrator.services.document_processor.AIRuntimeService.get_default_personality", return_value=None)
    @patch("ai_orchestrator.services.document_processor.AIRuntimeService.get_text_model", return_value="gemma-test")
    def test_empty_vision_output_is_failure_not_page_marker(self, model, personality):
        self.service.provider.execute_standard.return_value = {"response": ""}
        result = self.service.get_chat_extraction_result(b"image", "scan.png")
        self.assertEqual(result["text"], "")
        self.assertEqual(result["transcription_status"], "failed")

    def test_spreadsheet_preserves_zero_and_false(self):
        workbook = Workbook()
        workbook.active.append(["Revenue", 0, False])
        output = io.BytesIO()
        workbook.save(output)
        workbook.close()
        result = self.service.get_chat_extraction_result(output.getvalue(), "data.xlsx")
        self.assertIn("Revenue\t0\tFalse", result["text"])
