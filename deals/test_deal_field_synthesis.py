from unittest.mock import patch

from django.test import TestCase

from deals.models import Deal, DealDocument
from deals.services.deal_field_synthesis import DealFieldSynthesisService
from ai_orchestrator.models import AIAuditLog


class DealFieldSynthesisServiceTests(TestCase):
    def setUp(self):
        self.deal = Deal.objects.create(title="Field Synthesis Deal")
        self.document = DealDocument.objects.create(
            deal=self.deal,
            title="Pitch Deck.pdf",
            normalized_text="The company operates in logistics and seeks INR 75 Cr.",
            extracted_text="The company operates in logistics and seeks INR 75 Cr.",
            is_indexed=True,
            chunking_status="chunked",
            transcription_status="complete",
        )

    @patch("deals.services.deal_field_synthesis.EmbeddingService")
    @patch("deals.services.deal_field_synthesis.AIProcessorService")
    def test_fills_fields_only_after_indexing_and_is_idempotent(self, ai_cls, _embedding_cls):
        ai_cls.return_value.process_content.return_value = {
            "deal_model_data": {
                "title": self.deal.title,
                "industry": "Logistics",
                "sector": "Industrials",
                "funding_ask": "INR 75 Cr",
                "funding_ask_for": "Growth capital",
                "priority": None,
                "city": None,
                "state": None,
                "country": "India",
                "themes": ["Supply chain"],
                "is_female_led": None,
                "deal_summary": "Logistics growth-capital opportunity.",
                "deal_details": "Seeking INR 75 Cr.",
                "company_details": "Operates in logistics.",
                "priority_rationale": None,
            },
            "source_relationships": {
                "bank": {"name": None, "website_domain": None, "description": None},
                "primary_contact": None,
                "additional_contacts": [],
                "relationship_metadata": {
                    "source_type": "document",
                    "source_documents": ["Pitch Deck.pdf"],
                    "confidence": "High",
                    "ambiguities": [],
                },
            },
            "metadata": {
                "ambiguous_points": [],
                "documents_analyzed": ["Pitch Deck.pdf"],
                "missing_information_requests": [],
            },
        }

        first = DealFieldSynthesisService.synthesize(
            self.deal,
            batch_key="test:batch-1",
            source_type="onedrive_folder",
            required_document_ids=[str(self.document.id)],
        )
        replay = DealFieldSynthesisService.synthesize(
            self.deal,
            batch_key="test:batch-1",
            source_type="onedrive_folder",
            required_document_ids=[str(self.document.id)],
        )

        self.deal.refresh_from_db()
        self.assertEqual(first.id, replay.id)
        self.assertEqual(self.deal.industry, "Logistics")
        self.assertEqual(self.deal.funding_ask, "INR 75 Cr")
        self.assertEqual(self.deal.deal_summary, "Logistics growth-capital opportunity.")
        self.assertEqual(ai_cls.return_value.process_content.call_count, 1)

    def test_rejects_unindexed_batch_document(self):
        self.document.is_indexed = False
        self.document.save(update_fields=["is_indexed"])

        with self.assertRaisesMessage(ValueError, "before indexing completes"):
            DealFieldSynthesisService.synthesize(
                self.deal,
                batch_key="test:batch-2",
                source_type="email",
                required_document_ids=[str(self.document.id)],
            )


class FolderFieldSynthesisFinalizerTests(TestCase):
    @patch("deals.tasks.log_worker_event")
    def test_regular_vdr_finalizer_completes_only_after_field_synthesis(self, _log):
        from deals.tasks import finalize_folder_background

        deal = Deal.objects.create(title="Linked folder")
        audit = AIAuditLog.objects.create(
            source_type="vdr_indexing",
            source_id=str(deal.id),
            model_used="test-model",
            system_prompt="test",
            user_prompt="test",
            raw_response="",
            status="PROCESSING",
            is_success=False,
            source_metadata={"coverage_policy": "all_supported_files"},
        )
        with patch(
            "deals.services.deal_field_synthesis.DealFieldSynthesisService.synthesize",
            return_value=type("Analysis", (), {"id": "analysis-1"})(),
        ) as synthesize:
            result = finalize_folder_background.run(
                [{"status": "success", "document_id": "doc-1"}],
                str(deal.id),
                str(audit.id),
            )

        audit.refresh_from_db()
        deal.refresh_from_db()
        self.assertEqual(result["field_synthesis_analysis_id"], "analysis-1")
        self.assertEqual(audit.source_metadata["field_synthesis_status"], "completed")
        self.assertEqual(deal.processing_status, "completed")
        synthesize.assert_called_once()

    @patch("deals.tasks.broadcast_audit_log_update")
    @patch("deals.services.deal_field_synthesis.DealFieldSynthesisService.synthesize")
    def test_durable_vdr_finalizer_persists_field_synthesis_result(self, synthesize, _broadcast):
        from deals.tasks import finalize_durable_vdr_indexing

        deal = Deal.objects.create(title="Durable folder", processing_status="processing")
        audit = AIAuditLog.objects.create(
            source_type="vdr_indexing",
            source_id=str(deal.id),
            model_used="test-model",
            system_prompt="test",
            user_prompt="test",
            raw_response="",
            status="PROCESSING",
            is_success=False,
            source_metadata={
                "queue_state": "field_synthesis",
                "document_queue": [{"status": "completed", "document_id": "doc-1"}],
            },
        )
        synthesize.return_value = type("Analysis", (), {"id": "analysis-2"})()

        result = finalize_durable_vdr_indexing.run(str(audit.id))

        audit.refresh_from_db()
        deal.refresh_from_db()
        self.assertEqual(result["analysis_id"], "analysis-2")
        self.assertEqual(audit.status, "COMPLETED")
        self.assertEqual(audit.source_metadata["field_synthesis_status"], "completed")
        self.assertEqual(deal.processing_status, "completed")
