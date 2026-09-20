from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from ai_orchestrator.models import AIAuditLog
from ai_orchestrator.serializers import AIAuditLogSerializer
from deals.models import Deal, DealDocument
from deals.serializers import DealDocumentSerializer
from deals.services.vdr_failures import vdr_failure_message
from deals.tasks import _record_document_extraction_audit


class DocumentFailureVisibilityTests(TestCase):
    def setUp(self):
        self.deal = Deal.objects.create(title="Failure visibility")
        self.document = DealDocument.objects.create(
            deal=self.deal,
            title="Legacy NDA.doc",
            onedrive_id="legacy-doc",
            transcription_status="failed",
            error_message="LibreOffice could not read the legacy Word file.",
        )

    def test_document_serializer_exposes_processing_error(self):
        data = DealDocumentSerializer(self.document).data

        self.assertEqual(
            data["error_message"],
            "LibreOffice could not read the legacy Word file.",
        )

    @patch("deals.tasks.broadcast_audit_log_update")
    def test_failed_extraction_creates_child_audit_with_reason(self, broadcast):
        parent = AIAuditLog.objects.create(
            source_type="vdr_indexing",
            source_id=str(self.deal.id),
            context_label="VDR indexing: Failure visibility",
            model_used="vdr",
            system_prompt="index",
            user_prompt="index",
            status="PROCESSING",
        )

        _record_document_extraction_audit(
            self.document,
            {
                "mode": "docproc_remote",
                "transcription_status": "failed",
                "error": "No readable content was extracted.",
                "quality_flags": ["failed"],
            },
            parent_audit_id=str(parent.id),
            celery_task_id="document-task",
        )

        child = AIAuditLog.objects.get(source_type="document_extraction")
        self.assertEqual(child.status, "FAILED")
        self.assertEqual(child.error_message, "No readable content was extracted.")
        self.assertEqual(child.source_metadata["vdr_parent_audit_id"], str(parent.id))
        serialized = AIAuditLogSerializer(
            parent,
            context={"include_child_audits": True},
        ).data
        self.assertEqual(serialized["child_audits"][0]["id"], str(child.id))
        broadcast.assert_called_once_with(child, event_type="terminal", done=True)

    def test_parent_failure_message_contains_each_file_and_reason(self):
        message = vdr_failure_message([
            {"name": "First.doc", "error": "Conversion failed"},
            {"file": "Second.xls", "error": "Workbook is encrypted"},
        ])

        self.assertIn("First.doc: Conversion failed", message)
        self.assertIn("Second.xls: Workbook is encrypted", message)


class VDRFailureMessageTests(SimpleTestCase):
    def test_report_failure_message_uses_section_titles(self):
        message = vdr_failure_message(
            [{"title": "Key Financials", "error": "No verifiable citations."}],
            item_kind="report section",
        )

        self.assertEqual(
            message,
            "1 VDR report section failed. Key Financials: No verifiable citations.",
        )
