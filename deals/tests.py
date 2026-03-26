from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.test import TestCase

from deals.models import Deal
from deals.serializers import DealDetailSerializer
from deals.services.deal_creation import DealCreationService
from deals.services.folder_analysis import FolderAnalysisService


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

    @patch("deals.tasks.process_deal_folder_background.apply_async")
    def test_confirm_deal_from_session_backfills_missing_fields(self, mock_apply_async):
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
                "passed_files": [{"file_id": "file-1", "file_name": "Deck.pdf"}],
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
        self.assertEqual(
            analysis.analysis_json["metadata"]["analysis_input_files"],
            [{"file_id": "file-1", "file_name": "Deck.pdf"}],
        )

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
