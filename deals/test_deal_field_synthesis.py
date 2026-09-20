import json
from copy import deepcopy
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

    def _synthesis_result(self, *, industry="Logistics"):
        return {
            "deal_model_data": {
                "title": self.deal.title,
                "industry": industry,
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

    @patch("deals.services.deal_field_synthesis.EmbeddingService")
    @patch("deals.services.deal_field_synthesis.AIProcessorService")
    def test_fills_fields_only_after_indexing_and_is_idempotent(self, ai_cls, _embedding_cls):
        ai_cls.return_value.process_content.return_value = self._synthesis_result()

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

    def test_evidence_batches_keep_every_document_and_structured_value(self):
        self.document.evidence_json = {
            "document_name": "Pitch Deck.pdf",
            "claims": [f"pitch-claim-{index}" for index in range(20)],
            "metrics": [{"name": f"metric-{index}", "value": index} for index in range(20)],
        }
        self.document.save(update_fields=["evidence_json"])
        later_document = DealDocument.objects.create(
            deal=self.deal,
            title="Later Financials.xlsx",
            normalized_text="later-document-raw-evidence",
            evidence_json={
                "document_name": "Later Financials.xlsx",
                "claims": ["later-document-unique-claim"],
                "metrics": [{"name": "later-document-revenue", "value": "INR 42 Cr"}],
            },
            is_indexed=True,
            chunking_status="chunked",
            transcription_status="complete",
        )

        with patch.object(DealFieldSynthesisService, "MAX_CONTEXT_CHARS", 1_800), patch.object(
            DealFieldSynthesisService, "MAX_FRAGMENT_CHARS", 300,
        ):
            batches = DealFieldSynthesisService._evidence_batches(
                [self.document, later_document],
            )

        combined = "".join(batches)
        self.assertGreater(len(batches), 1)
        self.assertTrue(all(len(batch) <= 1_800 for batch in batches))
        self.assertIn("pitch-claim-19", combined)
        self.assertIn("later-document-unique-claim", combined)
        self.assertIn("later-document-revenue", combined)
        document_ids = {
            fragment["document_id"]
            for batch in batches
            for fragment in json.loads(batch)["evidence_fragments"]
        }
        self.assertEqual(document_ids, {str(self.document.id), str(later_document.id)})

    @patch("deals.services.deal_field_synthesis.EmbeddingService")
    @patch("deals.services.deal_field_synthesis.AIProcessorService")
    def test_multiple_evidence_batches_are_merged_before_persisting(self, ai_cls, _embedding_cls):
        candidate_one = self._synthesis_result(industry="Logistics")
        candidate_two = self._synthesis_result(industry="Transportation")
        merged = self._synthesis_result(industry="Logistics and Transportation")
        ai_cls.return_value.process_content.side_effect = [
            deepcopy(candidate_one),
            deepcopy(candidate_two),
            deepcopy(merged),
        ]

        with patch.object(
            DealFieldSynthesisService,
            "_evidence_batches",
            return_value=["evidence-batch-1", "evidence-batch-2"],
        ):
            DealFieldSynthesisService.synthesize(
                self.deal,
                batch_key="test:multi-batch",
                source_type="onedrive_folder",
                required_document_ids=[str(self.document.id)],
            )

        self.deal.refresh_from_db()
        calls = ai_cls.return_value.process_content.call_args_list
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0].kwargs["metadata"]["max_tokens"], 4_096)
        self.assertEqual(
            calls[2].kwargs["metadata"]["_source_metadata"]["field_synthesis_phase"],
            "candidate_reduce_0",
        )
        self.assertEqual(self.deal.industry, "Logistics and Transportation")

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
