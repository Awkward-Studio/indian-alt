from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from deals.services.document_artifacts import (
    DocumentArtifactCancelled,
    DocumentArtifactService,
    DocumentArtifactYielded,
)


class FullVDRArtifactTests(SimpleTestCase):
    @patch("deals.tasks.broadcast_audit_log_update")
    @patch("deals.tasks.AIRuntimeService.create_audit_log")
    @patch("deals.tasks.DocumentChunk.objects.filter")
    def test_document_embedding_has_its_own_completed_audit(
        self,
        chunk_filter,
        create_audit,
        broadcast,
    ):
        from deals.tasks import _vectorize_document_and_capture

        chunk_filter.return_value.count.return_value = 17
        audit = MagicMock()
        create_audit.return_value = audit
        document = MagicMock(
            id="document-1",
            title="Pebble IM.pdf",
            document_type="memo",
            deal_id="deal-1",
        )
        document.deal.id = "deal-1"
        document.deal.title = "Pebble"
        embed_service = MagicMock(
            model_name="embedding-model",
            chunk_size=1000,
            chunk_overlap=150,
        )
        embed_service.vectorize_document.return_value = True

        count = _vectorize_document_and_capture(
            document,
            embed_service,
            parent_audit_id="parent-1",
            celery_task_id="task-1",
        )

        self.assertEqual(count, 17)
        self.assertEqual(create_audit.call_args.kwargs["source_type"], "document_embedding")
        self.assertEqual(create_audit.call_args.kwargs["source_id"], "document-1")
        self.assertEqual(create_audit.call_args.kwargs["model_used"], "embedding-model")
        self.assertEqual(audit.status, "COMPLETED")
        self.assertEqual(audit.source_metadata["chunk_count"], 17)
        self.assertNotIn("tokens_used", audit.save.call_args.kwargs["update_fields"])
        broadcast.assert_called()

    def test_report_generation_has_no_wall_clock_deadline(self):
        from deals.tasks import generate_vdr_analysis_async

        self.assertEqual(generate_vdr_analysis_async.soft_time_limit, 0)
        self.assertEqual(generate_vdr_analysis_async.time_limit, 0)

    def test_document_waiting_has_no_wall_clock_deadline(self):
        from deals.tasks import process_single_document_async

        self.assertEqual(process_single_document_async.soft_time_limit, 0)
        self.assertEqual(process_single_document_async.time_limit, 0)

    @patch("deals.tasks.DealCreationService.apply_analysis_to_deal")
    @patch("deals.tasks.DealAnalysis.objects.create")
    @patch("ai_orchestrator.services.report_sections.ICReportSectionService.complete")
    @patch("ai_orchestrator.services.report_section_evidence.ICReportSectionEvidenceService")
    @patch("microsoft.services.email_ingestion_review.deal_email_evidence_gaps", return_value=[])
    @patch("deals.tasks.DocumentArtifactService.artifact_from_document")
    @patch("deals.tasks.DocumentArtifactService.artifact_status", return_value="complete")
    @patch("deals.tasks.DocumentArtifactService.artifact_complete", return_value=True)
    @patch("ai_orchestrator.services.ai_processor.AIProcessorService")
    @patch("deals.tasks.log_worker_event")
    def test_vdr_report_uses_sections_without_one_shot_report_call(
        self,
        _log_worker_event,
        ai_service_class,
        _artifact_complete,
        _artifact_status,
        artifact_from_document,
        _email_gaps,
        section_evidence_class,
        complete_sections,
        create_analysis,
        _apply_analysis,
    ):
        from ai_orchestrator.prompt_contracts import IC_REPORT_HEADERS
        from deals.tasks import synthesize_complete_deal_analysis

        report = "\n\n".join(f"{header}\n\nSection content with sufficient detail." for header in IC_REPORT_HEADERS)
        complete_sections.return_value = report
        section_evidence_class.return_value.covered_document_ids = {"document-1"}
        section_evidence_class.return_value.section_stats = {}
        section_evidence_class.return_value.total_selected_chunks = 0
        artifact_from_document.return_value = {"document_name": "Deck.pdf", "claims": []}
        created = MagicMock()
        create_analysis.return_value = created

        document = MagicMock(
            id="document-1",
            title="Deck.pdf",
            onedrive_id="file-1",
            document_type="pitch_deck",
            transcription_status="complete",
            evidence_json={},
            normalized_text="Extracted document evidence",
            extracted_text="Extracted document evidence",
        )
        deal = MagicMock(id="deal-1", title="Section Deal", sector="Consumer", industry="Food")
        for field in (
            "funding_ask", "funding_ask_for", "priority", "city", "state", "country",
            "comments", "deal_details", "company_details", "reasons_for_passing",
            "bank_name", "primary_contact_name", "priority_rationale",
        ):
            setattr(deal, field, None)
        for field in (
            "is_female_led", "management_meeting", "business_proposal_stage", "ic_stage",
        ):
            setattr(deal, field, False)
        deal.documents.all.return_value.order_by.return_value = [document]
        deal.analyses.order_by.return_value.first.return_value = None
        audit = MagicMock(id="audit-1")

        result = synthesize_complete_deal_analysis(deal, audit, force_regenerate=True)

        self.assertIs(result, created)
        ai_service_class.return_value.process_content.assert_not_called()
        self.assertEqual(complete_sections.call_args.kwargs["report"], "")
        self.assertEqual(
            complete_sections.call_args.kwargs["evidence_for_section"],
            section_evidence_class.return_value.retrieve,
        )
        self.assertTrue(complete_sections.call_args.kwargs["force_regenerate"])
        self.assertEqual(create_analysis.call_args.kwargs["thinking"], "")

    @override_settings(
        VDR_ARTIFACT_SEGMENT_WORKERS=1,
        VDR_ARTIFACT_SEGMENT_SOURCE_TOKENS=2_000,
        VDR_ARTIFACT_SEGMENT_OVERLAP_TOKENS=100,
    )
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
        self.assertIn("Build a complete structured evidence artifact", first_prompt)
        self.assertEqual(first_metadata["max_input_tokens"], 14336)
        self.assertEqual(first_metadata["max_tokens"], 45056)
        self.assertEqual(first_metadata["request_timeout"], 1800)

    def test_default_artifact_budget_leaves_room_for_complete_chat_envelope(self):
        from django.conf import settings

        provider_reserve = 4096
        envelope_margin = 2048
        self.assertLessEqual(
            settings.VDR_ARTIFACT_SEGMENT_INPUT_TOKENS
            + settings.VDR_ARTIFACT_SEGMENT_MAX_TOKENS
            + provider_reserve
            + envelope_margin,
            settings.CHAT_MODEL_CONTEXT_TOKENS,
        )

    def test_complete_artifact_is_reused_when_only_embedding_is_missing(self):
        from deals.tasks import _has_reusable_document_artifact

        artifact = DocumentArtifactService._fallback_artifact(
            file_name="Pebble IM.pdf",
            extracted_text="Complete source evidence",
            document_type="memo",
            extraction_mode="fallback_text",
        )
        artifact["quality_flags"] = []
        artifact["source_metadata"] = {
            "source_etag": "etag-1",
            "artifact_pipeline_version": DocumentArtifactService.ARTIFACT_PIPELINE_VERSION,
            "artifact_segment_count": 1,
            "artifact_segments_completed": 1,
        }
        document = MagicMock(
            is_indexed=False,
            normalized_text="Complete normalized evidence",
            extracted_text="Complete source evidence",
            evidence_json=artifact,
        )

        self.assertTrue(
            _has_reusable_document_artifact(
                document,
                source_etag="etag-1",
                force_fresh=False,
            )
        )
        self.assertFalse(
            _has_reusable_document_artifact(
                document,
                source_etag="etag-1",
                force_fresh=True,
            )
        )

    def test_segment_artifact_does_not_require_duplicated_normalized_text(self):
        fallback = DocumentArtifactService._fallback_artifact(
            file_name="Pebble Financial Model.xlsx",
            extracted_text="Raw source remains on the document checkpoint.",
            document_type="financials",
            extraction_mode="fallback_text",
        )
        parsed = {
            "document_name": "Pebble Financial Model.xlsx",
            "document_summary": "Revenue and profitability evidence from this workbook segment.",
            "normalized_text": "",
            "quality_flags": ["chat_direct_extraction"],
        }

        segment = DocumentArtifactService._normalize_segment_artifact(
            parsed,
            fallback=fallback,
        )

        self.assertNotIn("artifact_missing_text", segment["quality_flags"])
        self.assertTrue(DocumentArtifactService._segment_artifact_usable(segment))

    @override_settings(
        VDR_ARTIFACT_SEGMENT_WORKERS=1,
        VDR_ARTIFACT_SEGMENT_SOURCE_TOKENS=4_000,
        VDR_ARTIFACT_SEGMENT_OVERLAP_TOKENS=100,
    )
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

    @override_settings(
        VDR_ARTIFACT_SEGMENT_WORKERS=1,
        VDR_ARTIFACT_SEGMENT_SOURCE_TOKENS=4_000,
        VDR_ARTIFACT_SEGMENT_OVERLAP_TOKENS=100,
    )
    @patch("deals.services.document_artifacts.cache")
    def test_cancellation_stops_before_remaining_segments(self, mock_cache):
        mock_cache.get.return_value = None
        service = MagicMock()
        service.process_content.return_value = {"parsed_json": {"document_summary": "First"}}
        source_text = "section evidence " * 10_000

        with self.assertRaises(DocumentArtifactCancelled):
            DocumentArtifactService.build_document_artifact(
                file_name="Cancelled.pdf",
                extracted_text=source_text,
                ai_service=service,
                cancel_check=lambda: service.process_content.call_count >= 1,
            )

        self.assertEqual(service.process_content.call_count, 1)

    @override_settings(
        VDR_ARTIFACT_SEGMENT_SOURCE_TOKENS=4_000,
        VDR_ARTIFACT_SEGMENT_OVERLAP_TOKENS=100,
    )
    @patch("deals.services.document_artifacts.cache")
    def test_interactive_work_yields_before_next_uncached_segment(self, mock_cache):
        mock_cache.get.return_value = None
        service = MagicMock()
        service.process_content.return_value = {
            "parsed_json": {
                "document_summary": "Completed segment",
                "quality_flags": [],
            },
        }
        source_text = "section evidence " * 10_000

        with self.assertRaises(DocumentArtifactYielded):
            DocumentArtifactService.build_document_artifact(
                file_name="Long VDR.pdf",
                extracted_text=source_text,
                ai_service=service,
                yield_check=lambda: service.process_content.call_count >= 1,
            )

        self.assertEqual(service.process_content.call_count, 1)
        mock_cache.set.assert_called_once()

    @patch("deals.services.document_artifacts.cache")
    def test_poisoned_cached_segment_is_evicted_and_regenerated(self, mock_cache):
        mock_cache.get.return_value = {
            "document_summary": "Fallback",
            "quality_flags": ["fallback_artifact"],
        }
        service = MagicMock()
        service.process_content.return_value = {
            "parsed_json": {
                "document_name": "Retry.pdf",
                "document_summary": "Recovered evidence",
                "quality_flags": [],
            },
        }

        artifact = DocumentArtifactService.build_document_artifact(
            file_name="Retry.pdf",
            extracted_text="Complete source evidence for retry.",
            ai_service=service,
        )

        mock_cache.delete.assert_called_once()
        service.process_content.assert_called_once()
        self.assertEqual(DocumentArtifactService.artifact_status(artifact), "complete")

    @patch("deals.services.document_artifacts.cache")
    def test_provider_error_is_not_cached_as_segment_evidence(self, mock_cache):
        mock_cache.get.return_value = None
        service = MagicMock()
        service.process_content.return_value = {"error": "model request timed out"}

        artifact = DocumentArtifactService.build_document_artifact(
            file_name="Timeout.pdf",
            extracted_text="Complete source evidence that needs model processing.",
            ai_service=service,
        )

        mock_cache.set.assert_not_called()
        self.assertIn("artifact_segment_processing_incomplete", artifact["quality_flags"])

    def test_complete_artifact_remains_usable_with_partial_source_coverage(self):
        artifact = DocumentArtifactService._fallback_artifact(
            file_name="Mixed-content.pdf",
            extracted_text="Complete available text evidence.",
            document_type="Other",
            extraction_mode="fallback_text",
        )
        artifact["quality_flags"] = ["Page 1: embedded images were not interpreted."]
        artifact["source_metadata"] = {
            "artifact_segment_count": 1,
            "artifact_segments_completed": 1,
        }

        document = MagicMock()
        document.transcription_status = "partial"
        document.chunking_status = "not_chunked"
        document.evidence_json = artifact

        self.assertEqual(
            DocumentArtifactService.artifact_status(document),
            DocumentArtifactService.STATUS_PARTIAL,
        )
        self.assertEqual(
            DocumentArtifactService.artifact_status(document.evidence_json),
            DocumentArtifactService.STATUS_COMPLETE,
        )

    def test_segment_cache_is_scoped_to_the_user_started_vdr_run(self):
        shared = {
            "file_name": "Refresh.pdf",
            "segment": "same source",
            "index": 0,
            "total": 1,
            "model": "model",
        }

        first_run = DocumentArtifactService._segment_cache_key(**shared, run_scope="audit-1")
        retry_of_first_run = DocumentArtifactService._segment_cache_key(**shared, run_scope="audit-1")
        second_run = DocumentArtifactService._segment_cache_key(**shared, run_scope="audit-2")

        self.assertEqual(first_run, retry_of_first_run)
        self.assertNotEqual(first_run, second_run)

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
