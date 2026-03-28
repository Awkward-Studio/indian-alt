from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.test import TestCase

from ai_orchestrator.models import AIAuditLog, DocumentChunk
from deals.models import AnalysisKind, Deal, DealDocument, InitialAnalysisStatus
from deals.serializers import DealDetailSerializer
from deals.services.deal_creation import DealCreationService
from deals.services.folder_analysis import FolderAnalysisService
from deals.tasks import analyze_additional_documents_async, analyze_selection_async, process_single_document_async
from deals.services.phase_readiness import (
    DealPhaseReadinessService,
    PHASE_READINESS_SOURCE_TYPE,
)


class DealAnalysisMappingTests(TestCase):
    def setUp(self):
        self.analysis_json = {
            "deal_model_data": {
                "title": "Acme Finance",
                "industry": "NBFC",
                "sector": "Fintech",
                "funding_ask": "125",
                "funding_ask_for": "Growth capital",
                "priority": "High",
                "city": "Mumbai",
                "state": "Maharashtra",
                "country": "India",
                "themes": ["Digital Lending", "Embedded Finance", "", 42],
            },
            "metadata": {
                "ambiguous_points": [
                    "Customer concentration needs verification",
                    "Unit economics depend on channel mix",
                ]
            },
            "analyst_report": "Structured summary from AI",
            "thinking": "Reasoning trace",
        }

    def test_apply_analysis_to_deal_backfills_canonical_fields(self):
        deal = Deal.objects.create()

        DealCreationService.apply_analysis_to_deal(deal, self.analysis_json)

        deal.refresh_from_db()
        self.assertEqual(deal.title, "Acme Finance")
        self.assertEqual(deal.industry, "NBFC")
        self.assertEqual(deal.sector, "Fintech")
        self.assertEqual(deal.funding_ask, "125")
        self.assertEqual(deal.funding_ask_for, "Growth capital")
        self.assertEqual(deal.priority, "High")
        self.assertEqual(deal.city, "Mumbai")
        self.assertEqual(deal.state, "Maharashtra")
        self.assertEqual(deal.country, "India")
        self.assertEqual(deal.deal_summary, "Structured summary from AI")
        self.assertEqual(deal.themes, ["Digital Lending", "Embedded Finance"])

    def test_apply_analysis_to_deal_preserves_explicit_values(self):
        deal = Deal.objects.create(
            title="Manual Title",
            priority="Medium",
            funding_ask="80",
            themes=["Existing Theme"],
        )

        DealCreationService.apply_analysis_to_deal(deal, self.analysis_json)

        deal.refresh_from_db()
        self.assertEqual(deal.title, "Manual Title")
        self.assertEqual(deal.priority, "Medium")
        self.assertEqual(deal.funding_ask, "80")
        self.assertEqual(deal.themes, ["Existing Theme"])
        self.assertEqual(deal.industry, "NBFC")
        self.assertEqual(deal.city, "Mumbai")

    def test_process_deal_creation_creates_analysis_and_maps_ambiguities(self):
        deal = Deal.objects.create()

        DealCreationService.process_deal_creation(
            deal,
            {"analysis_json": self.analysis_json},
        )

        deal.refresh_from_db()
        analysis = deal.latest_analysis
        self.assertIsNotNone(analysis)
        self.assertEqual(analysis.analysis_kind, AnalysisKind.INITIAL)
        self.assertEqual(analysis.thinking, "Reasoning trace")
        self.assertEqual(
            analysis.ambiguities,
            [
                "Customer concentration needs verification",
                "Unit economics depend on channel mix",
            ],
        )
        self.assertEqual(deal.funding_ask, "125")
        self.assertEqual(deal.themes, ["Digital Lending", "Embedded Finance"])
        self.assertEqual(
            deal.ambiguities,
            [
                "Customer concentration needs verification",
                "Unit economics depend on channel mix",
            ],
        )

    def test_detail_serializer_includes_latest_analysis_fields(self):
        deal = Deal.objects.create(title="Acme Finance")

        DealCreationService.process_deal_creation(
            deal,
            {"analysis_json": self.analysis_json},
        )

        serialized = DealDetailSerializer(instance=deal).data

        self.assertEqual(serialized["thinking"], "Reasoning trace")
        self.assertEqual(serialized["initial_analysis"]["kind"], "initial")
        self.assertEqual(serialized["current_analysis"]["canonical_snapshot"]["analyst_report"], "Structured summary from AI")
        self.assertEqual(
            serialized["ambiguities"],
            [
                "Customer concentration needs verification",
                "Unit economics depend on channel mix",
            ],
        )
        self.assertEqual(serialized["analysis_json"]["metadata"]["ambiguous_points"][0], "Customer concentration needs verification")
        self.assertEqual(serialized["analysis_history"][0]["ambiguities"][1], "Unit economics depend on channel mix")

    def test_detail_serializer_returns_empty_ambiguities_without_analysis(self):
        deal = Deal.objects.create(title="No Analysis Yet")

        serialized = DealDetailSerializer(instance=deal).data

        self.assertEqual(serialized["ambiguities"], [])
        self.assertEqual(serialized["analysis_json"], {})
        self.assertEqual(serialized["analysis_history"], [])
        self.assertIsNone(serialized["latest_phase_readiness_check"])

    def test_detail_serializer_includes_latest_phase_readiness_check(self):
        deal = Deal.objects.create(
            title="Acme Finance",
            current_phase="4: Initial Materials Review",
        )
        AIAuditLog.objects.create(
            source_type=PHASE_READINESS_SOURCE_TYPE,
            source_id=str(deal.id),
            context_label="Phase Readiness: Acme Finance",
            model_used="qwen3.5:latest",
            system_prompt="Queued phase-readiness recommendation...",
            user_prompt="Evaluate phase readiness",
            status="COMPLETED",
            is_success=True,
            parsed_json={
                "decision": "ready",
                "is_ready_for_next_phase": True,
                "recommended_next_phase": "5: Financial Model Call",
                "rationale": "Materials review is complete and no blocking gaps remain.",
                "blocking_gaps": [],
                "evidence_signals": ["Strong initial materials quality"],
            },
        )

        serialized = DealDetailSerializer(instance=deal).data

        self.assertEqual(serialized["latest_phase_readiness_check"]["status"], "COMPLETED")
        self.assertEqual(
            serialized["latest_phase_readiness_check"]["parsed_json"]["recommended_next_phase"],
            "5: Financial Model Call",
        )

    def test_phase_readiness_normalize_result_preserves_exact_blockers(self):
        deal = Deal.objects.create(
            title="NDA Hold",
            current_phase="3: NDA Execution",
        )

        normalized = DealPhaseReadinessService.normalize_result(
            {
                "decision": "not_ready",
                "is_ready_for_next_phase": False,
                "recommended_next_phase": None,
                "rationale": "The deal cannot move ahead because NDA completion is not evidenced in the saved record.",
                "blocking_gaps": [
                    "Missing signed NDA from both parties; no executed NDA is recorded in the saved deal context.",
                    "No evidence confidential materials were shared after NDA completion, so the phase gate remains unproven.",
                ],
                "evidence_signals": [
                    "Current phase is 3: NDA Execution.",
                ],
            },
            deal,
        )

        self.assertEqual(
            normalized["blocking_gaps"],
            [
                "Missing signed NDA from both parties; no executed NDA is recorded in the saved deal context.",
                "No evidence confidential materials were shared after NDA completion, so the phase gate remains unproven.",
            ],
        )

    def test_phase_readiness_normalize_result_backfills_missing_blockers_for_not_ready(self):
        deal = Deal.objects.create(
            title="Model Review",
            current_phase="5: Financial Model Call",
        )

        normalized = DealPhaseReadinessService.normalize_result(
            {
                "decision": "not_ready",
                "is_ready_for_next_phase": False,
                "recommended_next_phase": None,
                "rationale": "The model walkthrough evidence is not sufficient.",
                "blocking_gaps": [],
                "evidence_signals": [],
            },
            deal,
        )

        self.assertEqual(
            normalized["blocking_gaps"],
            [
                "The saved deal context does not show that the gate for 5: Financial Model Call has been cleared."
            ],
        )

    def test_phase_readiness_normalize_result_backfills_missing_blockers_for_insufficient_information(self):
        deal = Deal.objects.create(
            title="Diligence Pending",
            current_phase="13: Full Due Diligence",
        )

        normalized = DealPhaseReadinessService.normalize_result(
            {
                "decision": "insufficient_information",
                "is_ready_for_next_phase": False,
                "recommended_next_phase": None,
                "rationale": "The record is too sparse to confirm diligence completion.",
                "blocking_gaps": [],
                "evidence_signals": [],
            },
            deal,
        )

        self.assertEqual(
            normalized["blocking_gaps"],
            [
                "The saved deal context is missing enough phase-specific evidence to determine whether 13: Full Due Diligence is cleared."
            ],
        )

    @patch("ai_orchestrator.services.embedding_processor.EmbeddingService.vectorize_document")
    @patch("deals.tasks.process_deal_folder_background.apply_async")
    def test_confirm_deal_from_session_backfills_missing_fields(self, mock_apply_async, mock_vectorize_document):
        mock_vectorize_document.return_value = True
        session_id = "session-123"
        cache.set(
            f"folder_sync_{session_id}",
            {
                "folder_id": "folder-1",
                "drive_id": "drive-1",
                "file_tree": [{"id": "file-1", "name": "Deck.pdf"}],
                "user_email": "analyst@example.com",
                "preview_text": "Combined extracted text",
                "raw_thinking": "Folder reasoning trace",
                "passed_files": [{
                    "file_id": "file-1",
                    "file_name": "Deck.pdf",
                    "extracted_text": "Deck extracted text",
                    "extraction_mode": "glm_ocr",
                    "transcription_status": "complete",
                }],
                "approved_file_ids": ["file-1"],
                "failed_files": [{"file_id": "file-2", "file_name": "Broken.pdf", "reason": "Unreadable"}],
                "preliminary_data": self.analysis_json,
            },
            timeout=3600,
        )
        deal = Deal.objects.create(title="Manual Title")

        result = FolderAnalysisService.confirm_deal_from_session(session_id, deal)

        self.assertEqual(result["status"], "success")
        mock_apply_async.assert_not_called()

        deal.refresh_from_db()
        analysis = deal.latest_analysis
        self.assertEqual(deal.title, "Manual Title")
        self.assertEqual(deal.funding_ask, "125")
        self.assertEqual(deal.city, "Mumbai")
        self.assertEqual(deal.deal_summary, "Structured summary from AI")
        self.assertEqual(deal.themes, ["Digital Lending", "Embedded Finance"])
        self.assertEqual(deal.extracted_text, "Combined extracted text")
        self.assertEqual(deal.processing_status, "idle")
        self.assertEqual(analysis.thinking, "Folder reasoning trace")
        self.assertEqual(analysis.analysis_kind, AnalysisKind.INITIAL)
        self.assertEqual(
            analysis.analysis_json["metadata"]["analysis_input_files"],
            [{"file_id": "file-1", "file_name": "Deck.pdf"}],
        )
        self.assertEqual(deal.documents.count(), 2)
        passed_doc = deal.documents.get(onedrive_id="file-1")
        self.assertEqual(passed_doc.initial_analysis_status, InitialAnalysisStatus.SELECTED_AND_ANALYZED)
        self.assertEqual(passed_doc.transcription_status, "complete")
        self.assertEqual(passed_doc.extraction_mode, "glm_ocr")
        failed_doc = deal.documents.get(onedrive_id="file-2")
        self.assertEqual(failed_doc.initial_analysis_status, InitialAnalysisStatus.SELECTED_FAILED)

    @patch("ai_orchestrator.services.ai_processor.AIProcessorService")
    def test_analyze_selection_async_returns_real_preliminary_data(self, mock_ai_processor):
        cache.set(
            "folder_sync_session-1",
            {
                "folder_id": "folder-1",
                "drive_id": "drive-1",
                "user_email": "analyst@example.com",
                "passed_files": [
                    {"file_id": "keep-1", "file_name": "Deck.pdf", "extracted_text": "Important content"},
                    {"file_id": "skip-1", "file_name": "Model.xlsx", "extracted_text": "Old content"},
                ],
                "failed_files": [{"file_id": "bad-1", "file_name": "Broken.pdf", "reason": "Unreadable"}],
            },
            timeout=3600,
        )
        audit_log = AIAuditLog.objects.create(
            source_type="onedrive_folder",
            source_id="folder-1",
            model_used="qwen3.5:latest",
            system_prompt="queued",
            user_prompt="queued",
            status="PENDING",
            is_success=False,
        )
        mock_ai_processor.return_value.process_content.return_value = {
            "parsed_json": {"analyst_report": "Fresh report", "metadata": {}},
            "thinking": "New reasoning",
        }

        task_self = MagicMock()
        task_self.request.id = "task-1"
        result = analyze_selection_async(task_self, "session-1", str(audit_log.id), ["keep-1"])

        self.assertEqual(result["preliminary_data"]["analyst_report"], "Fresh report")
        self.assertEqual(result["passed_files"], [{"file_id": "keep-1", "file_name": "Deck.pdf", "extracted_text": "Important content"}])
        audit_log.refresh_from_db()
        self.assertEqual(
            audit_log.source_metadata["analysis_input_files"],
            [{"file_id": "keep-1", "file_name": "Deck.pdf", "extracted_text": "Important content"}],
        )

    @patch("deals.tasks._vectorize_document_and_capture")
    @patch("deals.tasks.GraphAPIService")
    @patch("deals.tasks.DocumentProcessorService")
    @patch("ai_orchestrator.services.ai_processor.AIProcessorService")
    def test_analyze_additional_documents_async_retranscribes_partial_docs(
        self,
        mock_ai_processor,
        mock_doc_processor,
        mock_graph_service,
        mock_vectorize,
    ):
        deal = Deal.objects.create(title="Incremental", source_drive_id="drive-1", deal_summary="v1")
        doc = DealDocument.objects.create(
            deal=deal,
            title="Deck.pdf",
            document_type="Pitch Deck",
            onedrive_id="file-1",
            extracted_text="preview",
            transcription_status="partial",
            chunking_status="not_chunked",
            is_ai_analyzed=False,
        )
        audit_log = AIAuditLog.objects.create(
            source_type="vdr_incremental_analysis",
            source_id=str(deal.id),
            model_used="qwen3.5:latest",
            system_prompt="queued",
            user_prompt="queued",
            status="PENDING",
            is_success=False,
        )
        mock_graph_service.return_value.get_drive_item_content.return_value = b"pdf"
        mock_doc_processor.return_value.get_extraction_result.return_value = {
            "text": "full extracted text from OCR",
            "mode": "glm_ocr",
        }
        mock_vectorize.return_value = 3
        mock_ai_processor.return_value.process_content.return_value = {
            "analyst_report": "Version 2 findings",
            "metadata": {},
        }

        task_self = MagicMock()
        task_self.request.id = "task-2"
        result = analyze_additional_documents_async(task_self, str(deal.id), [str(doc.id)], str(audit_log.id))

        self.assertEqual(result["status"], "success")
        doc.refresh_from_db()
        self.assertEqual(doc.extracted_text, "full extracted text from OCR")
        self.assertEqual(doc.transcription_status, "complete")
        self.assertEqual(doc.extraction_mode, "glm_ocr")
        self.assertEqual(doc.is_ai_analyzed, True)
        self.assertEqual(deal.latest_analysis.analysis_kind, AnalysisKind.SUPPLEMENTAL)
        self.assertIn("canonical_snapshot", deal.latest_analysis.analysis_json)
        audit_log.refresh_from_db()
        self.assertEqual(audit_log.source_metadata["file_diagnostics"][0]["chunk_count"], 3)

    @patch("deals.tasks._vectorize_document_and_capture")
    @patch("deals.tasks.DocumentProcessorService")
    @patch("deals.tasks.GraphAPIService")
    def test_process_single_document_async_marks_preview_as_partial(
        self,
        mock_graph_service,
        mock_doc_processor,
        mock_vectorize,
    ):
        deal = Deal.objects.create(title="Preview Deal")
        mock_graph_service.return_value.get_drive_item_content.return_value = b"pdf"
        mock_doc_processor.return_value.get_extraction_result.return_value = {
            "text": "preview text from first pages",
            "mode": "glm_ocr",
        }
        mock_vectorize.return_value = 2

        task_self = MagicMock()
        task_self.request.id = "task-3"
        result = process_single_document_async(
            task_self,
            {"id": "file-1", "name": "Deck.pdf", "driveId": "drive-1"},
            str(deal.id),
            "analyst@example.com",
            True,
            None,
        )

        self.assertEqual(result["status"], "success")
        doc = DealDocument.objects.get(onedrive_id="file-1")
        self.assertEqual(doc.transcription_status, "partial")
        self.assertEqual(doc.is_ai_analyzed, False)

    @patch("ai_orchestrator.models.AIAuditLog.objects.filter")
    @patch("deals.tasks.process_deal_folder_background.apply_async")
    def test_trigger_vdr_processing_queues_from_audit_log_metadata(self, mock_apply_async, mock_filter):
        mock_apply_async.return_value = MagicMock(id="task-123")

        deal = Deal.objects.create(
            title="Deferred VDR",
            source_onedrive_id="folder-1",
            source_drive_id="drive-1",
        )

        mock_log = MagicMock()
        mock_log.source_metadata = {
            "drive_id": "drive-1",
            "file_tree": [{"id": "file-1", "name": "Deck.pdf"}],
        }
        mock_queryset = MagicMock()
        mock_queryset.order_by.return_value = [mock_log]
        mock_filter.return_value = mock_queryset

        result = FolderAnalysisService.trigger_vdr_processing(deal)

        self.assertEqual(result["status"], "queued")
        mock_apply_async.assert_called_once()
        deal.refresh_from_db()
        self.assertEqual(deal.processing_status, "processing")

    def test_trigger_vdr_processing_requires_persisted_folder_metadata(self):
        deal = Deal.objects.create(title="No Folder Context")

        result = FolderAnalysisService.trigger_vdr_processing(deal)

        self.assertIn("error", result)
