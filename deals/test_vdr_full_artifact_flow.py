from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from deals.services.document_artifacts import DocumentArtifactService


class FullVDRArtifactTests(SimpleTestCase):
    def test_document_task_allows_complete_multi_segment_analysis_window(self):
        from deals.tasks import process_single_document_async

        self.assertEqual(process_single_document_async.soft_time_limit, 5100)
        self.assertEqual(process_single_document_async.time_limit, 5400)

    @override_settings(VDR_ARTIFACT_SEGMENT_WORKERS=1)
    @patch("deals.services.document_artifacts.cache")
    def test_every_segment_is_analyzed_and_merged_into_full_artifact(self, mock_cache):
        mock_cache.get.return_value = None
        service = MagicMock()

        def response(**kwargs):
            content = kwargs["content"]
            return {
                "parsed_json": {
                    "document_name": "Model.xlsx",
                    "document_type": "Financial Model",
                    "document_type_suggestion": {
                        "label": "financial_model",
                        "display_label": "Financial Model",
                        "confidence": "High",
                        "rationale": "Financial evidence",
                    },
                    "document_summary": content[-40:],
                    "claims": [content[-20:]],
                    "metrics": [],
                    "numeric_evidence": [],
                    "table_definitions": [],
                    "tables_summary": [],
                    "contacts_found": [],
                    "risks": [],
                    "open_questions": [],
                    "diligence_gaps": [],
                    "citations": ["Model.xlsx"],
                    "industry_overview": {"findings": [], "market_figures": [], "citations": []},
                    "quality_flags": [],
                    "source_map": {},
                }
            }

        service.process_content.side_effect = response
        source_text = "A" * 15_500
        expected_segments = DocumentArtifactService._split_for_artifact(source_text)

        artifact = DocumentArtifactService.build_document_artifact(
            file_name="Model.xlsx",
            extracted_text=source_text,
            document_type="Financial Model",
            ai_service=service,
            source_metadata={"source_file_id": "file-1"},
        )

        self.assertEqual(service.process_content.call_count, len(expected_segments))
        self.assertEqual(artifact["normalized_text"], source_text)
        self.assertEqual(artifact["source_metadata"]["artifact_segments_completed"], len(expected_segments))
        self.assertEqual(DocumentArtifactService.artifact_status(artifact), DocumentArtifactService.STATUS_COMPLETE)
        first_prompt = service.process_content.call_args_list[0].kwargs["content"]
        first_metadata = service.process_content.call_args_list[0].kwargs["metadata"]
        self.assertIn("INTERNAL-DOCUMENT-EVIDENCE-EXTRACTION", first_prompt)
        self.assertIn("Extract structured internal-document evidence", first_prompt)
        self.assertEqual(first_metadata["request_timeout"], 600)

    @override_settings(VDR_ARTIFACT_SEGMENT_WORKERS=1)
    @patch("deals.services.document_artifacts.cache")
    def test_failed_segment_cannot_be_marked_as_complete(self, mock_cache):
        from deals.services.document_artifacts import DocumentArtifactService

        mock_cache.get.return_value = None
        service = MagicMock()
        service.process_content.side_effect = [
            {"parsed_json": {"document_summary": "First segment"}},
            RuntimeError("model request failed"),
        ]
        long_text = ("first section evidence " * 400) + ("second section evidence " * 400)

        artifact = DocumentArtifactService.build_document_artifact(
            file_name="Long memo.pdf",
            extracted_text=long_text,
            ai_service=service,
        )

        self.assertIn("artifact_segment_processing_incomplete", artifact["quality_flags"])
        self.assertEqual(
            DocumentArtifactService.artifact_status(artifact),
            DocumentArtifactService.STATUS_DEGRADED,
        )

    @patch("deals.tasks.synthesize_complete_deal_analysis")
    @patch("deals.tasks._is_cancel_requested", return_value=False)
    @patch("ai_orchestrator.models.AIAuditLog.objects.get")
    @patch("deals.tasks.Deal.objects.get")
    def test_artifact_finalizer_waits_for_user_confirmation(
        self,
        mock_deal_get,
        mock_audit_get,
        _mock_cancel,
        mock_synthesize,
    ):
        from deals.tasks import finalize_folder_background

        deal = MagicMock(processing_status="processing", processing_error=None)
        audit = MagicMock(source_metadata={})
        mock_deal_get.return_value = deal
        mock_audit_get.return_value = audit

        result = finalize_folder_background.run(
            [{"status": "success", "file": "Deck.pdf"}],
            "deal-1",
            "audit-1",
        )

        mock_synthesize.assert_not_called()
        self.assertEqual(deal.processing_status, "completed")
        self.assertTrue(result["analysis_confirmation_required"])
        self.assertEqual(audit.source_metadata["workflow_stage"], "artifacts_ready")
