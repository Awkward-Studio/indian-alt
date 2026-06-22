import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
import requests


from django.core.management import call_command
from django.core.cache import cache
from django.test import TestCase
from django.contrib.auth.models import User
from rest_framework.test import APIClient
from django.urls import reverse

from ai_orchestrator.models import AIAuditLog, DealRetrievalProfile, DocumentChunk
from ai_orchestrator.services.embedding_processor import EmbeddingService
from deals.models import (
    AnalysisKind, Deal, DealAnalysis, DealDocument, InitialAnalysisStatus,
    VentureIntelligenceCompanyProfile, VentureIntelligenceFinancialStatement, VentureIntelligenceCompanyRelation,
    VentureIntelligenceExecutive, VentureIntelligencePEInvestment, VentureIntelligenceAngelInvestment,
    VentureIntelligenceIncubationInvestment, VentureIntelligencePEExit, VentureIntelligencePEIPO,
    VentureIntelligenceMergerAcquisition, VentureIntelligenceEpfoData, VentureIntelligenceSimilarCompany
)
from deals.serializers import DealDetailSerializer, DealSerializer
from contacts.serializers import ContactSerializer
from contacts.models import Contact
from banks.models import Bank
from deals.services.deal_creation import DealCreationService
from deals.services.document_artifacts import DocumentArtifactService
from deals.services.deal_flow import DealFlowService
from deals.services.contact_linking import sync_contact_deal_links
from deals.services.folder_analysis import FolderAnalysisService
from deals.services.bulk_sync_resolution import folder_aliases, resolve_existing_deal, synthesis_canonical_title
from deals.tasks import (
    analyze_additional_documents_async,
    fetch_company_news_async_task,
    analyze_selection_async,
    fetch_competitors_async_task,
    process_single_document_async,
)
from deals.services.venture_intelligence import VentureIntelligenceService


class CompetitorSearchPipelineTests(TestCase):
    @patch("deals.tasks.EmbeddingService")
    @patch("ai_orchestrator.services.llm_providers.AnthropicProviderService")
    def test_company_news_search_persists_dated_memo_document(self, mock_provider, mock_embedding_service):
        deal = Deal.objects.create(
            title="Acme Commerce",
            sector="Consumer",
            industry="Ecommerce",
            city="Bengaluru",
            country="India",
            deal_summary="Online commerce platform.",
        )
        provider = mock_provider.return_value
        provider.execute_standard.return_value = {
            "response": json.dumps({
                "overview": "Acme has recent public-domain diligence news.",
                "executive_summary": "Acme has recent public-domain diligence news.",
                "news_cards": [{
                    "title": "Acme raised growth capital",
                    "date": "2026-01-15",
                    "summary": "Acme announced a new funding round.",
                    "category": "funding",
                    "sentiment": "green",
                    "source": "Example News",
                    "url": "https://example.com/acme-funding",
                }],
                "funding": [],
                "litigation": [],
                "patents": [],
                "founders": [],
                "awards": [],
                "red_flags": [],
                "green_flags": [{
                    "title": "Acme won an award",
                    "date": "2026-02-01",
                    "summary": "Acme received industry recognition.",
                    "source": "Example Awards",
                    "url": "https://example.com/acme-award",
                }],
                "other": [],
                "sources": [{
                    "title": "Funding report",
                    "publisher": "Example News",
                    "date": "2026-01-15",
                    "url": "https://example.com/acme-funding",
                }],
            })
        }
        mock_embedding_service.return_value.vectorize_document.return_value = True

        first = fetch_company_news_async_task(str(deal.id))
        second = fetch_company_news_async_task(str(deal.id))

        prompt = provider.execute_standard.call_args.args[0]["prompt"].lower()
        self.assertIn("web search", prompt)
        self.assertIn("funding", prompt)
        self.assertIn("litigation", prompt)
        self.assertIn("founder", prompt)
        self.assertIn("awards", prompt)
        self.assertIn("red/green flags", prompt)
        self.assertIn("at most 5 news_cards", prompt)
        self.assertEqual(provider.execute_standard.call_args.args[0]["options"]["max_search_uses"], 1)
        self.assertEqual(provider.execute_standard.call_args.args[0]["options"]["max_tokens"], 1200)

        docs = DealDocument.objects.filter(deal=deal, title__startswith="Public Domain News Research").order_by("created_at")
        self.assertEqual(docs.count(), 2)
        self.assertEqual(docs.first().document_type, "Memo")
        self.assertIn("Acme raised growth capital", docs.first().normalized_text)
        self.assertEqual(mock_embedding_service.return_value.vectorize_document.call_count, 2)
        self.assertEqual(first["counts"]["green_flags"], 1)
        self.assertEqual(first["news_cards"][0]["title"], "Acme raised growth capital")
        self.assertEqual(second["document"]["title"], docs.last().title)

    @patch("ai_orchestrator.services.llm_providers.AnthropicProviderService")
    def test_initial_competitor_search_defers_cin_resolution(self, mock_provider):
        deal = Deal.objects.create(
            title="Acme Commerce",
            sector="Consumer",
            industry="Ecommerce",
            city="Bengaluru",
            country="India",
            deal_summary="Online commerce platform.",
        )
        provider = mock_provider.return_value
        provider.execute_standard.return_value = {
            "response": json.dumps({
                "competitors": [
                    {
                        "company_name": "Peer Commerce",
                        "core_business": "Online marketplace",
                        "nature_of_competition": "Direct category peer",
                        "country_or_region": "India",
                    }
                ]
            })
        }

        result = fetch_competitors_async_task(str(deal.id))

        prompt = provider.execute_standard.call_args.args[0]["prompt"]
        self.assertNotIn('"cin"', prompt.lower())
        self.assertNotIn("Prefer companies with VI/MCA-compatible Indian CINs", prompt)
        self.assertIn("Do not search for or return Corporate Identity Numbers", prompt)
        self.assertEqual(result["competitors"], [{
            "name": "Peer Commerce",
            "notes": "Core Business: Online marketplace\nNature Of Competition: Direct category peer",
            "cin": "",
        }])
        self.assertNotIn("CIN:", result["response"])


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
        self.assertFalse(deal.title)
        self.assertEqual(deal.industry, "NBFC")
        self.assertEqual(deal.sector, "Fintech")
        self.assertEqual(deal.funding_ask, "125")
        self.assertEqual(deal.funding_ask_for, "Growth capital")
        self.assertEqual(deal.priority, "Medium")
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

    def test_apply_analysis_to_deal_never_overwrites_title_even_with_overwrite(self):
        deal = Deal.objects.create(title="Canonical Title")

        DealCreationService.apply_analysis_to_deal(deal, self.analysis_json, overwrite=True)

        deal.refresh_from_db()
        self.assertEqual(deal.title, "Canonical Title")
        self.assertEqual(deal.industry, "NBFC")

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

    def test_normalize_analysis_payload_preserves_document_evidence_in_canonical_snapshot(self):
        normalized = DealCreationService.normalize_analysis_payload(
            {
                "deal_model_data": {"title": "Acme Finance"},
                "analyst_report": "Report body",
                "document_evidence": [{"document_name": "Deck.pdf"}],
                "metadata": {"ambiguous_points": ["Check margin bridge"]},
            },
            analysis_kind=AnalysisKind.INITIAL,
            documents_analyzed=["Deck.pdf"],
            analysis_input_files=[{"file_id": "file-1", "file_name": "Deck.pdf"}],
            failed_files=[],
        )

        self.assertEqual(normalized["document_evidence"], [{"document_name": "Deck.pdf"}])
        self.assertEqual(
            normalized["canonical_snapshot"]["document_evidence"],
            [{"document_name": "Deck.pdf"}],
        )
        self.assertEqual(normalized["metadata"]["documents_analyzed"], ["Deck.pdf"])

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

    def test_detail_serializer_includes_persisted_file_tree_for_folder_backed_deal(self):
        deal = Deal.objects.create(
            title="Folder Backed Deal",
            source_onedrive_id="folder-123",
            source_drive_id="drive-xyz",
        )
        AIAuditLog.objects.create(
            source_type="onedrive_folder",
            source_id="folder-123",
            context_label="Folder: Backed",
            model_used="qwen3.5:latest",
            system_prompt="Traversal complete",
            user_prompt="Analyze folder",
            status="COMPLETED",
            is_success=True,
            source_metadata={
                "drive_id": "drive-xyz",
                "folder_id": "folder-123",
                "file_tree": [
                    {"id": "file-1", "name": "Deck.pdf", "path": "Investment/Deck.pdf"},
                ],
            },
        )

        serialized = DealDetailSerializer(instance=deal).data

        self.assertEqual(serialized["file_tree"][0]["name"], "Deck.pdf")
        self.assertEqual(serialized["file_tree"][0]["path"], "Investment/Deck.pdf")


class DealStatusSyncTests(TestCase):
    def test_serializer_create_syncs_deal_status_and_current_phase(self):
        serializer = DealSerializer(data={
            "title": "Synced Create",
            "deal_status": "12: Term Sheet",
        })

        self.assertTrue(serializer.is_valid(), serializer.errors)
        deal = serializer.save()

        self.assertEqual(deal.deal_status, "12: Term Sheet")
        self.assertEqual(deal.current_phase, "12: Term Sheet")

    def test_serializer_update_syncs_from_current_phase(self):
        deal = Deal.objects.create(
            title="Synced Update",
            deal_status="3: NDA Execution",
            current_phase="3: NDA Execution",
        )
        serializer = DealSerializer(instance=deal, data={"current_phase": "16: IC Note II"}, partial=True)

        self.assertTrue(serializer.is_valid(), serializer.errors)
        updated = serializer.save()

        self.assertEqual(updated.deal_status, "16: IC Note II")
        self.assertEqual(updated.current_phase, "16: IC Note II")

    def test_deal_serializer_sets_primary_contact_bank_and_additional_contacts(self):
        bank = Bank.objects.create(name="Axis Capital")
        primary_contact = Contact.objects.create(name="Primary Banker", bank=bank)
        secondary_contact = Contact.objects.create(name="Secondary Banker")
        serializer = DealSerializer(data={
            "title": "Linked Deal",
            "primary_contact": str(primary_contact.id),
            "additional_contacts": [str(secondary_contact.id)],
        })

        self.assertTrue(serializer.is_valid(), serializer.errors)
        deal = serializer.save()

        self.assertEqual(deal.primary_contact_id, primary_contact.id)
        self.assertEqual(deal.bank_id, bank.id)
        self.assertEqual(list(deal.additional_contacts.values_list("id", flat=True)), [secondary_contact.id])
        self.assertEqual(deal.other_contacts, [str(secondary_contact.id)])
    def test_contact_serializer_updates_linked_deals_bidirectionally(self):
        bank = Bank.objects.create(name="Avendus")
        contact = Contact.objects.create(name="Banker", bank=bank)
        deal_primary = Deal.objects.create(title="Primary Deal")
        deal_additional = Deal.objects.create(title="Additional Deal")

        serializer = ContactSerializer(
            instance=contact,
            data={
                "bank": str(bank.id),
                "linked_deals_payload": [
                    {"deal_id": str(deal_primary.id), "is_primary": True},
                    {"deal_id": str(deal_additional.id), "is_primary": False},
                ],
            },
            partial=True,
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()

        deal_primary.refresh_from_db()
        deal_additional.refresh_from_db()
        self.assertEqual(deal_primary.primary_contact_id, contact.id)
        self.assertEqual(deal_primary.bank_id, bank.id)
        self.assertTrue(deal_additional.additional_contacts.filter(id=contact.id).exists())

    def test_sync_contact_deal_links_removes_unselected_relationships(self):
        contact = Contact.objects.create(name="Relationship Banker")
        deal = Deal.objects.create(title="Legacy Linked", primary_contact=contact)

        sync_contact_deal_links(contact, [])

        deal.refresh_from_db()
        self.assertIsNone(deal.primary_contact)

    def test_update_flow_state_sets_passed_on_rejection(self):
        deal = Deal.objects.create(
            title="Rejected Deal",
            deal_status="5: Financial Model Call",
            current_phase="5: Financial Model Call",
        )

        DealFlowService.update_flow_state(
            deal=deal,
            active_stage="Passed",
            decisions_update={"5": "no"},
            reason="Model assumptions broke",
            rejection_stage_id=5,
        )

        deal.refresh_from_db()
        self.assertEqual(deal.deal_status, "Passed")
        self.assertEqual(deal.current_phase, "Passed")
        self.assertEqual(deal.rejection_stage_id, 5)

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
                    "normalized_text": "Deck normalized text",
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
        self.assertEqual(deal.extracted_text, "--- DOCUMENT: Deck.pdf ---\nDeck normalized text")
        self.assertEqual(deal.processing_status, "idle")
        self.assertEqual(analysis.thinking, "Folder reasoning trace")
        self.assertEqual(analysis.analysis_kind, AnalysisKind.INITIAL)
        self.assertEqual(
            analysis.analysis_json["metadata"]["analysis_input_files"],
            [{"file_id": "file-1", "file_name": "Deck.pdf"}],
        )
        self.assertEqual(
            analysis.analysis_json["metadata"]["documents_analyzed"],
            ["Deck.pdf"],
        )
        self.assertEqual(deal.documents.count(), 2)
        passed_doc = deal.documents.get(onedrive_id="file-1")
        self.assertEqual(passed_doc.initial_analysis_status, InitialAnalysisStatus.SELECTED_AND_ANALYZED)
        self.assertEqual(passed_doc.transcription_status, "complete")
        self.assertEqual(passed_doc.extraction_mode, "glm_ocr")
        failed_doc = deal.documents.get(onedrive_id="file-2")
        self.assertEqual(failed_doc.initial_analysis_status, InitialAnalysisStatus.SELECTED_FAILED)

    @patch("ai_orchestrator.services.embedding_processor.EmbeddingService.vectorize_document")
    def test_confirm_deal_from_session_returns_existing_confirmed_deal(self, mock_vectorize_document):
        mock_vectorize_document.return_value = True
        existing_deal = Deal.objects.create(title="Existing")
        origin_log = AIAuditLog.objects.create(
            source_type="onedrive_folder",
            source_id="folder-1",
            context_label="Selection Analysis",
            status="COMPLETED",
            is_success=True,
            source_metadata={"deal_id": str(existing_deal.id)},
        )
        session_id = "session-existing"
        cache.set(
            f"folder_sync_{session_id}",
            {
                "originating_audit_log_id": str(origin_log.id),
                "preliminary_data": self.analysis_json,
                "passed_files": [],
                "failed_files": [],
            },
            timeout=3600,
        )
        duplicate = Deal.objects.create(title="Duplicate")

        result = FolderAnalysisService.confirm_deal_from_session(session_id, duplicate)

        self.assertEqual(result["deal_id"], existing_deal.id)
        self.assertEqual(result["message"], "Deal already created from this analysis session.")
        self.assertFalse(Deal.objects.filter(id=duplicate.id).exists())

    @patch("ai_orchestrator.services.embedding_processor.EmbeddingService.vectorize_document")
    def test_confirm_deal_from_session_restores_folder_linkage_from_origin_log(self, mock_vectorize_document):
        mock_vectorize_document.return_value = True
        origin_log = AIAuditLog.objects.create(
            source_type="onedrive_folder",
            source_id="folder-xyz",
            context_label="Selection Analysis",
            status="COMPLETED",
            is_success=True,
            source_metadata={
                "folder_id": "folder-xyz",
                "drive_id": "drive-xyz",
                "file_tree": [{"id": "file-1", "name": "Deck.pdf", "path": "Deck.pdf"}],
                "analysis_input_files": [{"file_id": "file-1", "file_name": "Deck.pdf"}],
            },
        )
        session_id = "session-origin-log"
        cache.set(
            f"folder_sync_{session_id}",
            {
                "originating_audit_log_id": str(origin_log.id),
                "preliminary_data": self.analysis_json,
                "passed_files": [{
                    "file_id": "file-1",
                    "file_name": "Deck.pdf",
                    "extracted_text": "Deck extracted text",
                    "extraction_mode": "glm_ocr",
                    "transcription_status": "complete",
                }],
                "approved_file_ids": ["file-1"],
                "failed_files": [],
            },
            timeout=3600,
        )
        deal = Deal.objects.create(title="Linked Later")

        result = FolderAnalysisService.confirm_deal_from_session(session_id, deal)

        self.assertEqual(result["status"], "success")
        deal.refresh_from_db()
        self.assertEqual(deal.source_onedrive_id, "folder-xyz")
        self.assertEqual(deal.source_drive_id, "drive-xyz")

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
        self.assertIn("document_evidence", result["preliminary_data"])
        self.assertEqual(result["preliminary_data"]["metadata"]["documents_analyzed"], ["Deck.pdf"])
        self.assertEqual(len(result["passed_files"]), 1)
        self.assertEqual(result["passed_files"][0]["file_id"], "keep-1")
        self.assertEqual(result["passed_files"][0]["file_name"], "Deck.pdf")
        self.assertEqual(result["passed_files"][0]["normalized_text"], "Important content")
        self.assertIn("document_artifact", result["passed_files"][0])
        audit_log.refresh_from_db()
        self.assertEqual(len(audit_log.source_metadata["analysis_input_files"]), 1)
        self.assertEqual(audit_log.source_metadata["analysis_input_files"][0]["file_id"], "keep-1")
        self.assertEqual(audit_log.source_metadata["analysis_input_files"][0]["file_name"], "Deck.pdf")

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
        self.assertIn(doc.normalized_text or "", ["preview text from first pages", doc.extracted_text])

    def test_document_artifact_service_reports_complete_and_degraded_statuses(self):
        deal = Deal.objects.create(title="Artifact Deal")
        complete_doc = DealDocument.objects.create(
            deal=deal,
            title="Complete.pdf",
            document_type="Other",
            extracted_text="Some extracted text",
            normalized_text="Some normalized text",
            evidence_json={
                "document_name": "Complete.pdf",
                "document_type": "Other",
                "document_summary": "Summary",
                "claims": [],
                "metrics": [],
                "tables_summary": [],
                "contacts_found": [],
                "risks": [],
                "open_questions": [],
                "citations": ["Complete.pdf"],
                "reasoning": "",
                "quality_flags": [],
                "normalized_text": "Some normalized text",
                "source_map": {"document_name": "Complete.pdf"},
            },
        )
        degraded_doc = DealDocument.objects.create(
            deal=deal,
            title="Fallback.pdf",
            document_type="Other",
            extracted_text="Fallback text",
            normalized_text="Fallback text",
            evidence_json={
                "document_name": "Fallback.pdf",
                "document_type": "Other",
                "document_summary": "Fallback text",
                "claims": [],
                "metrics": [],
                "tables_summary": [],
                "contacts_found": [],
                "risks": [],
                "open_questions": [],
                "citations": ["Fallback.pdf"],
                "reasoning": "",
                "quality_flags": ["fallback_artifact"],
                "normalized_text": "Fallback text",
                "source_map": {"document_name": "Fallback.pdf"},
            },
        )

        self.assertTrue(DocumentArtifactService.artifact_complete(complete_doc))
        self.assertEqual(DocumentArtifactService.artifact_status(degraded_doc), DocumentArtifactService.STATUS_DEGRADED)

    def test_document_serializer_exposes_artifact_status(self):
        deal = Deal.objects.create(title="Serializer Deal")
        doc = DealDocument.objects.create(
            deal=deal,
            title="Evidence.pdf",
            document_type="Other",
            extracted_text="Source text",
            normalized_text="Source text",
            evidence_json=DocumentArtifactService.artifact_from_file_record(
                {"file_name": "Evidence.pdf", "extracted_text": "Source text", "document_type": "Other"}
            ),
        )

        serialized = DealDetailSerializer(instance=deal).data
        self.assertEqual(serialized["documents"][0]["artifact_status"], DocumentArtifactService.STATUS_DEGRADED)
        self.assertFalse(serialized["documents"][0]["artifact_complete"])

    def test_document_artifact_service_builds_embedding_chunk_families(self):
        artifact = {
            "document_name": "Metrics.pdf",
            "document_type": "Pitch Deck",
            "document_summary": "Revenue and EBITDA overview",
            "claims": ["Growth accelerated in FY25"],
            "metrics": [{"name": "EBITDA Margin", "value": "19%"}],
            "tables_summary": [{"title": "P&L", "rows": ["Revenue", "EBITDA"]}],
            "contacts_found": [],
            "risks": ["Customer concentration remains high"],
            "open_questions": [],
            "citations": ["Metrics.pdf"],
            "reasoning": "",
            "quality_flags": [],
            "normalized_text": "Normalized narrative text",
            "source_map": {"document_name": "Metrics.pdf"},
        }

        chunks = DocumentArtifactService.build_embedding_chunks(artifact)
        kinds = [chunk["metadata"]["chunk_kind"] for chunk in chunks]
        self.assertIn("normalized_text", kinds)
        self.assertIn("metric", kinds)
        self.assertIn("table_summary", kinds)
        self.assertIn("claim", kinds)
        self.assertIn("risk", kinds)

    def test_vectorize_document_creates_multiple_chunk_families(self):
        deal = Deal.objects.create(title="Chunked Deal")
        doc = DealDocument.objects.create(
            deal=deal,
            title="Deck.pdf",
            document_type="Pitch Deck",
            extracted_text="Revenue grew quickly and EBITDA margin improved.",
            normalized_text="Revenue grew quickly and EBITDA margin improved.",
            evidence_json={
                "document_name": "Deck.pdf",
                "document_type": "Pitch Deck",
                "document_summary": "Revenue and profitability improved",
                "claims": ["Revenue momentum improved"],
                "metrics": [{"name": "EBITDA Margin", "value": "19%"}],
                "tables_summary": [{"title": "Financial Summary", "values": ["Revenue", "EBITDA"]}],
                "contacts_found": [],
                "risks": ["Concentration risk"],
                "open_questions": [],
                "citations": ["Deck.pdf"],
                "reasoning": "",
                "quality_flags": [],
                "normalized_text": "Revenue grew quickly and EBITDA margin improved.",
                "source_map": {"document_name": "Deck.pdf"},
            },
            source_map_json={"document_name": "Deck.pdf"},
            key_metrics_json=[{"name": "EBITDA Margin", "value": "19%"}],
            table_json=[{"title": "Financial Summary"}],
        )

        service = EmbeddingService()
        service.is_sqlite = True

        self.assertTrue(service.vectorize_document(doc))
        created_chunks = list(DocumentChunk.objects.filter(deal=deal, source_type='document', source_id=str(doc.id)))
        self.assertGreaterEqual(len(created_chunks), 4)
        chunk_kinds = {chunk.metadata.get("chunk_kind") for chunk in created_chunks}
        self.assertIn("normalized_text", chunk_kinds)
        self.assertIn("metric", chunk_kinds)
        self.assertIn("table_summary", chunk_kinds)

    def test_rerank_prefers_metric_chunks_for_numeric_queries(self):
        deal = Deal.objects.create(title="Ranking Deal")
        service = EmbeddingService()
        service.is_sqlite = True
        metric_chunk = DocumentChunk(
            deal=deal,
            source_type='document',
            source_id='doc-1',
            content='EBITDA Margin: 19%',
            metadata={"chunk_kind": "metric"},
        )
        text_chunk = DocumentChunk(
            deal=deal,
            source_type='document',
            source_id='doc-1',
            content='General company overview',
            metadata={"chunk_kind": "normalized_text"},
        )

        reranked = service._rerank_chunks([text_chunk, metric_chunk], "What is EBITDA margin?", 2)
        self.assertEqual(reranked[0].metadata.get("chunk_kind"), "metric")

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


class BulkSyncResolutionAliasTests(TestCase):
    def test_synthesis_canonical_title_prefers_folder_identity_over_synthesized_title(self):
        artifact = {
            "deal_name": "Folder Backed Canonical Name",
            "portable_deal_data": {
                "deal_model_data": {
                    "title": "Investment Report: Renamed By Synthesis",
                }
            },
        }

        canonical = synthesis_canonical_title(artifact, "Folder_Backed_Canonical_Name")

        self.assertEqual(canonical, "Folder Backed Canonical Name")

    def test_folder_aliases_keeps_synthesized_title_only_as_compatibility_alias(self):
        artifact = {
            "deal_name": "Folder Backed Canonical Name",
            "portable_deal_data": {
                "deal_model_data": {
                    "title": "Investment Report: Renamed By Synthesis",
                }
            },
        }

        aliases = folder_aliases("Folder_Backed_Canonical_Name", artifact)

        self.assertEqual(aliases[0], "Folder Backed Canonical Name")
        self.assertIn("Investment Report: Renamed By Synthesis", aliases)

    def test_resolve_existing_deal_falls_back_to_distinctive_title_token(self):
        deal = Deal.objects.create(title="Example Target Holdings Private Limited")
        artifact = {
            "deal_name": "Example",
            "portable_deal_data": {"deal_model_data": {"title": "Example Target Holdings"}},
        }

        resolution = resolve_existing_deal("Example", artifact)

        self.assertEqual(resolution.deal, deal)
        self.assertEqual(resolution.matched_by, "fuzzy:Example")

    def test_resolve_existing_deal_avoids_ambiguous_fuzzy_matches(self):
        Deal.objects.create(title="Example Alpha Private Limited")
        Deal.objects.create(title="Example Beta Private Limited")
        artifact = {
            "deal_name": "Example",
            "portable_deal_data": {"deal_model_data": {"title": None}},
        }

        resolution = resolve_existing_deal("Example", artifact)

        self.assertIsNone(resolution.deal)


class RebuildDerivedDealStateCommandTests(TestCase):
    def _write_synthesis_fixture(self, base_dir: Path, folder_name: str, artifact: dict, report_text: str):
        deal_dir = base_dir / folder_name
        deal_dir.mkdir(parents=True, exist_ok=True)
        (deal_dir / "DEAL_SYNTHESIS.artifact.json").write_text(json.dumps(artifact), encoding="utf-8")
        (deal_dir / "INVESTMENT_REPORT.md").write_text(report_text, encoding="utf-8")
        return deal_dir

    @patch("deals.management.commands.rebuild_derived_deal_state.refresh_deal_embeddings")
    def test_rebuild_command_repairs_title_and_rebuilds_derived_state(self, mock_refresh_embeddings):
        deal = Deal.objects.create(
            title="Investment Report: Acme Finance",
            current_phase="5: Financial Model Call",
            deal_status="5: Financial Model Call",
            deal_summary="Old summary",
            funding_ask="999",
            industry="Old Industry",
            sector="Old Sector",
            city="Old City",
            state="Old State",
            country="Old Country",
            priority="High",
            deal_details="Old deal details",
            company_details="Old company details",
            priority_rationale="Old rationale",
            themes=["Old Theme"],
            legacy_investment_bank="Old Bank",
            extracted_text="Raw document corpus",
            is_indexed=True,
        )
        DealAnalysis.objects.create(
            deal=deal,
            version=1,
            analysis_kind=AnalysisKind.INITIAL,
            thinking="old thinking",
            ambiguities=["old ambiguity"],
            analysis_json={"deal_model_data": {"title": "Old Title"}, "analyst_report": "Old summary"},
        )
        DocumentChunk.objects.create(
            deal=deal,
            source_type="extracted_source",
            source_id="doc-1",
            content="Preserve me",
            metadata={"chunk_kind": "normalized_text", "chunk_index": 0},
        )
        DocumentChunk.objects.create(
            deal=deal,
            source_type="deal_summary",
            source_id=str(deal.id),
            content="Old derived summary",
            metadata={"title": deal.title, "chunk_index": 0},
        )
        DealRetrievalProfile.objects.create(
            deal=deal,
            profile_text="Old retrieval profile",
            embedding_model="test",
            metadata={"title": deal.title},
        )

        def fake_refresh_embeddings(refreshed_deal, embed_service=None):
            DocumentChunk.objects.create(
                deal=refreshed_deal,
                source_type="deal_summary",
                source_id=str(refreshed_deal.id),
                content=refreshed_deal.deal_summary,
                metadata={"title": refreshed_deal.title, "chunk_index": 0, "total_chunks": 1},
            )
            DealRetrievalProfile.objects.update_or_create(
                deal=refreshed_deal,
                defaults={
                    "profile_text": f"profile::{refreshed_deal.title}",
                    "embedding_model": "test",
                    "metadata": {"title": refreshed_deal.title},
                },
            )
            refreshed_deal.is_indexed = True
            refreshed_deal.save(update_fields=["is_indexed"])
            return True, True

        mock_refresh_embeddings.side_effect = fake_refresh_embeddings

        artifact = {
            "deal_name": "Acme Finance",
            "thinking_process": "Fresh synthesis reasoning",
            "portable_deal_data": {
                "deal_model_data": {
                    "title": "Acme Finance",
                    "industry": "NBFC",
                    "sector": "Fintech",
                    "funding_ask": "125",
                    "funding_ask_for": "Growth capital",
                    "priority": "Medium",
                    "city": "Mumbai",
                    "state": "Maharashtra",
                    "country": "India",
                    "themes": ["Digital Lending", "Embedded Finance"],
                    "deal_details": "Fresh deal details",
                    "company_details": "Fresh company details",
                    "priority_rationale": "Fresh rationale",
                },
                "metadata": {
                    "ambiguous_points": ["Verify collection efficiency"],
                    "documents_analyzed": ["Deck.pdf"],
                    "analysis_input_files": [{"file_name": "Deck.pdf"}],
                    "failed_files": [],
                },
                "analyst_report": "Artifact report body",
            },
            "metadata": {
                "documents_used": [{"document_name": "Deck.pdf", "document_type": "Pitch Deck"}],
                "documents_used_count": 1,
            },
        }
        report_text = "## Executive Summary\n\nThis is the rebuilt markdown report."

        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            self._write_synthesis_fixture(base_dir, "Acme_Finance", artifact, report_text)

            call_command(
                "rebuild_derived_deal_state",
                "--apply",
                "--base-dir",
                str(base_dir),
            )

        deal.refresh_from_db()
        self.assertEqual(deal.title, "Acme Finance")
        self.assertEqual(deal.current_phase, "5: Financial Model Call")
        self.assertEqual(deal.deal_status, "5: Financial Model Call")
        self.assertEqual(deal.funding_ask, "125")
        self.assertEqual(deal.industry, "NBFC")
        self.assertEqual(deal.sector, "Fintech")
        self.assertEqual(deal.city, "Mumbai")
        self.assertEqual(deal.priority, "Medium")
        self.assertEqual(deal.deal_summary, report_text)
        self.assertEqual(deal.themes, ["Digital Lending", "Embedded Finance"])
        self.assertEqual(deal.extracted_text, "Raw document corpus")

        self.assertEqual(deal.analyses.count(), 1)
        rebuilt_analysis = deal.latest_analysis
        self.assertEqual(rebuilt_analysis.version, 1)
        self.assertEqual(rebuilt_analysis.analysis_kind, AnalysisKind.INITIAL)
        self.assertEqual(rebuilt_analysis.thinking, "Fresh synthesis reasoning")

        self.assertEqual(
            DocumentChunk.objects.filter(deal=deal, source_type="extracted_source").count(),
            1,
        )
        self.assertGreater(
            DocumentChunk.objects.filter(deal=deal, source_type="deal_summary").count(),
            0,
        )
        self.assertEqual(DealRetrievalProfile.objects.filter(deal=deal).count(), 1)
        mock_refresh_embeddings.assert_called_once()

    def test_rebuild_command_dry_run_does_not_mutate(self):
        deal = Deal.objects.create(
            title="Investment Report: Dry Run Finance",
            deal_summary="Old summary",
            funding_ask="999",
            current_phase="1: Deal Sourced",
            deal_status="1: Deal Sourced",
        )
        DealAnalysis.objects.create(
            deal=deal,
            version=1,
            analysis_kind=AnalysisKind.INITIAL,
            thinking="old thinking",
            ambiguities=[],
            analysis_json={"deal_model_data": {"title": "Old Title"}, "analyst_report": "Old summary"},
        )
        DocumentChunk.objects.create(
            deal=deal,
            source_type="deal_summary",
            source_id=str(deal.id),
            content="Old derived summary",
            metadata={"title": deal.title, "chunk_index": 0},
        )
        DealRetrievalProfile.objects.create(
            deal=deal,
            profile_text="Old retrieval profile",
            embedding_model="test",
            metadata={"title": deal.title},
        )

        artifact = {
            "deal_name": "Dry Run Finance",
            "portable_deal_data": {
                "deal_model_data": {
                    "title": "Dry Run Finance",
                    "industry": "Lending",
                },
                "metadata": {"ambiguous_points": []},
                "analyst_report": "New report",
            },
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            self._write_synthesis_fixture(base_dir, "Dry_Run_Finance", artifact, "## New report")

            call_command(
                "rebuild_derived_deal_state",
                "--base-dir",
                str(base_dir),
            )

        deal.refresh_from_db()
        self.assertEqual(deal.title, "Investment Report: Dry Run Finance")
        self.assertEqual(deal.deal_summary, "Old summary")
        self.assertEqual(deal.funding_ask, "999")
        self.assertEqual(deal.analyses.count(), 1)
        self.assertEqual(DocumentChunk.objects.filter(deal=deal, source_type="deal_summary").count(), 1)
        self.assertEqual(DealRetrievalProfile.objects.filter(deal=deal).count(), 1)

    def test_rebuild_command_prunes_deals_without_synthesis_artifacts(self):
        matched_deal = Deal.objects.create(title="Matched Finance")
        unmatched_deal = Deal.objects.create(title="Unmatched Finance")
        artifact = {
            "deal_name": "Matched Finance",
            "portable_deal_data": {
                "deal_model_data": {"title": "Matched Finance"},
                "metadata": {"ambiguous_points": []},
                "analyst_report": "Matched report",
            },
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            self._write_synthesis_fixture(base_dir, "Matched_Finance", artifact, "## Matched report")

            call_command(
                "rebuild_derived_deal_state",
                "--apply",
                "--prune-unmatched-deals",
                "--prune-only",
                "--base-dir",
                str(base_dir),
            )

        self.assertTrue(Deal.objects.filter(id=matched_deal.id).exists())
        self.assertFalse(Deal.objects.filter(id=unmatched_deal.id).exists())


class VentureIntelligenceServiceTests(TestCase):
    def setUp(self):
        self.deal = Deal.objects.create(
            title="Flipkart",
            industry="Old Industry",
            sector="Old Sector",
            city="Old City"
        )
        self.service = VentureIntelligenceService()
        # Always use mock API key so tests never hit the live API.
        # Use `manage.py test_vi_connection --test-store` for live validation.
        self.service.api_key = "test-api-key"

        self.embedding_patcher = patch(
            "ai_orchestrator.services.embedding_processor.EmbeddingService._get_embedding",
            return_value=[0.1] * 1024
        )
        self.mock_get_embedding = self.embedding_patcher.start()

    def tearDown(self):
        self.embedding_patcher.stop()

    @patch("deals.services.venture_intelligence.requests.post")
    def test_fetch_company_details_success(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "results": {"profile": {"name": "Flipkart"}}}
        mock_post.return_value = mock_response

        data = self.service.fetch_company_details(company_name="Flipkart")
        self.assertTrue(data["success"])
        self.assertEqual(data["results"]["profile"]["name"], "Flipkart")
        mock_post.assert_called_once()

    @patch("deals.services.venture_intelligence.requests.post")
    def test_fetch_company_details_not_found(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_post.return_value = mock_response

        with self.assertRaises(ValueError):
            self.service.fetch_company_details(company_name="NonExistent")

    @patch("deals.services.venture_intelligence.AIProcessorService.process_content")
    def test_resolve_cin_via_ai(self, mock_process_content):
        mock_process_content.return_value = {
            "response": '{"cin": "U74999KA2012PTC066107", "entity_name": "Flipkart Private Limited", "confidence": 0.95}'
        }
        res = self.service.resolve_cin_via_ai("Flipkart")
        self.assertEqual(res["cin"], "U74999KA2012PTC066107")
        self.assertEqual(res["entity_name"], "Flipkart Private Limited")

    @patch("deals.services.venture_intelligence.requests.post")
    @patch("deals.services.venture_intelligence.AIProcessorService.process_content")
    def test_enrich_deal_target(self, mock_process_content, mock_post):
        # Mock AI resolution to return a CIN
        mock_process_content.return_value = {
            "response": '{"cin": "U74999KA2012PTC066107", "entity_name": "Flipkart Private Limited"}'
        }
        
        # Mock VI API response
        mock_vi_response = MagicMock()
        mock_vi_response.status_code = 200
        mock_vi_response.json.return_value = {
            "success": True,
            "results": {
                "profile": {
                    "cin": "U74999KA2012PTC066107",
                    "name": "Flipkart",
                    "registered_name": "Flipkart Private Limited",
                    "website": "https://flipkart.com",
                    "industry": "Retail",
                    "sector": "E-Commerce",
                    "email": "contact@flipkart.com",
                    "year_founded": "2007",
                    "city": {"name": "Bengaluru"},
                    "total_funding": "3000",
                    "management_info": [
                        {"name": "Kalyan Krishnamurthy", "designation": "CEO", "belongs_to_firm_name": "Flipkart"}
                    ],
                    "board_info": [
                        {"name": "Sachin Bansal", "designation": "Director", "belongs_to_firm_name": "Flipkart Board"}
                    ]
                },
                "profit_loss": [
                    {"fy": "FY23", "fin_type": "Consolidated", "revenue": "10000"}
                ],
                "balance_sheet": [
                    {"fy": "FY23", "fin_type": "Consolidated", "assets": "5000"}
                ],
                "cash_flow": [
                    {"fy": "FY23", "fin_type": "Consolidated", "operating_cash": "800"}
                ]
            }
        }
        
        # Always use mocks — live API testing is done via `manage.py test_vi_connection --test-store`
        mock_post.side_effect = [Exception("Not found directly"), mock_vi_response]

        # Call enrich
        profile = self.service.enrich_deal(deal_id=self.deal.id, company_name="Flipkart")

        self.assertEqual(profile.cin, "U74999KA2012PTC066107")
        self.assertEqual(profile.name, "Flipkart")
        self.assertEqual(profile.industry, "Retail")
        self.assertEqual(profile.sector, "E-Commerce")

        # Check profiles, statements, relations created
        self.assertEqual(VentureIntelligenceCompanyProfile.objects.count(), 1)
        self.assertEqual(VentureIntelligenceFinancialStatement.objects.count(), 3)
        self.assertEqual(VentureIntelligenceCompanyRelation.objects.count(), 1)

        relation = VentureIntelligenceCompanyRelation.objects.first()
        self.assertEqual(relation.deal, self.deal)
        self.assertEqual(relation.relation_type, "target")

        # Check Deal update
        self.deal.refresh_from_db()
        self.assertEqual(self.deal.industry, "Retail")
        self.assertEqual(self.deal.sector, "E-Commerce")
        self.assertEqual(self.deal.city, "Bengaluru")
        self.assertEqual(self.deal.company_details, "Flipkart Private Limited")

        # Check Contacts created & linked
        self.assertEqual(Contact.objects.count(), 2)
        primary = self.deal.primary_contact
        self.assertIsNotNone(primary)
        self.assertEqual(primary.name, "Kalyan Krishnamurthy")
        self.assertEqual(primary.designation, "CEO")
        self.assertEqual(self.deal.additional_contacts.count(), 2)

    @patch("deals.services.venture_intelligence.requests.post")
    @patch("deals.services.venture_intelligence.AIProcessorService.process_content")
    def test_enrich_deal_full_schema(self, mock_process_content, mock_post):
        # Mock AI resolution to return a CIN
        mock_process_content.return_value = {
            "response": '{"cin": "U74999KA2012PTC066107", "entity_name": "Flipkart Private Limited"}'
        }

        # Mock VI API response with full nested schema
        mock_vi_response = MagicMock()
        mock_vi_response.status_code = 200
        mock_vi_response.json.return_value = {
            "success": True,
            "results": {
                "profile": {
                    "cin": "U74999KA2012PTC066107",
                    "name": "Flipkart",
                    "registered_name": "Flipkart Private Limited",
                    "website": "https://flipkart.com",
                    "industry": "Retail",
                    "sector": "E-Commerce",
                    "email": "contact@flipkart.com",
                    "year_founded": "2007",
                    "city": {"name": "Bengaluru", "state": "Karnataka", "region": "South", "country": "India"},
                    "total_funding": "3000",
                    "telephone": "123456",
                    "phone": "987654",
                    "linkedin": "https://linkedin.com/company/flipkart",
                    "tags": "ecommerce,retail,tech",
                    "listing_status": "Unlisted",
                    "additional_info": "Some extra notes here",
                    "management_info": [
                        {"name": "Kalyan Krishnamurthy", "designation": "CEO", "belongs_to_firm_name": "Flipkart"}
                    ],
                    "board_info": [
                        {"name": "Sachin Bansal", "designation": "Director", "belongs_to_firm_name": "Flipkart Board"}
                    ]
                },
                "cfs_profile": {
                    "short_name": "FK",
                    "previous_name": "Flipkart Online",
                    "full_name": "Flipkart Private Limited",
                    "business_description": "Online ecommerce store in India.",
                    "transacted_status": "Active",
                    "incorp_year": 2007,
                    "company_status": "Active",
                    "address": "Building 8",
                    "address_line2": "Tech Park",
                    "contact_name": "Kalyan",
                    "contact_designation": "CEO",
                    "auditor_name": "PwC",
                    "shp_year": 2023,
                    "shp_promoter": 15.5,
                    "shp_non_promoter": 84.5,
                    "is_xbrl": True,
                    "pincode": "560001",
                    "epfo_data": [
                        {"qrtr": "2023-Q1", "employees": 15000}
                    ]
                },
                "private_equity": {
                    "pe_investments": [
                        {
                            "round": "Series H",
                            "deal_date": "2021-07-01",
                            "amount": "3600",
                            "amount_inr": "26000",
                            "investors": ["GIC", "CPPIB", "SoftBank"],
                            "exit_status": "Active",
                            "company_valuation_post_money": "37600",
                            "revenue_multiple_post_money": "5.2",
                            "is_vc": "No",
                            "is_amount_hide": False,
                            "is_debt_deal": False,
                            "is_agg_hide": False
                        }
                    ],
                    "angel_investments": [
                        {
                            "date": "2009-01-01",
                            "investors": ["Angel Investor A"],
                            "is_exited": True,
                            "is_agg_hide": False
                        }
                    ],
                    "incubation_investments": [
                        {
                            "date": "2008-01-01",
                            "status": "Graduated",
                            "incubator": "Accelerator X"
                        }
                    ],
                    "pe_exits": [
                        {
                            "deal_type": "Secondary Sale",
                            "date": "2018-05-18",
                            "exit_investors": ["Tiger Global"],
                            "amount": "16000",
                            "exit_status": "Exited",
                            "valuation": "20000",
                            "revenue_multiple": "3.5",
                            "is_vc": False,
                            "is_hide_amount": False
                        }
                    ],
                    "pe_ipos": [
                        {
                            "date": "2025-12-01",
                            "ipo_investors": ["Public"],
                            "ipo_size": "5000",
                            "is_investor_sale": True,
                            "ipo_valuation": "40000",
                            "is_amount_hide": False,
                            "is_vc": False
                        }
                    ]
                },
                "merger_acquisition": [
                    {
                        "company": "Myntra",
                        "date": "2014-05-01",
                        "amount": "300",
                        "acquirer": "Flipkart",
                        "company_valuation": "300",
                        "company_valuation_post": "300",
                        "revenue_multiple": "1.2",
                        "revenue_multiple_post": "1.2",
                        "is_hide_amount": False,
                        "is_asset_sale": False,
                        "is_minority_deal": False
                    }
                ],
                "similar_cos": [
                    {
                        "name": "Amazon India",
                        "cin": "U51909KA2011FTC060489",
                        "sector": "E-Commerce",
                        "total_funding": "5000",
                        "latest_investment": {"round": "Corporate Round", "date": "2022-01-01", "amount": "1000"},
                        "city": "Bengaluru"
                    }
                ],
                "profit_loss": [
                    {"fy": "FY23", "fin_type": "Consolidated", "revenue": "10000"}
                ]
            }
        }
        
        # Always use mocks — live API testing is done via `manage.py test_vi_connection --test-store`
        mock_post.side_effect = [Exception("Not found directly"), mock_vi_response]

        # Call enrich
        profile = self.service.enrich_deal(deal_id=self.deal.id, company_name="Flipkart")

        # 1. Assert profile data
        self.assertEqual(profile.cin, "U74999KA2012PTC066107")
        self.assertEqual(profile.name, "Flipkart")
        self.assertEqual(profile.state, "Karnataka")
        self.assertEqual(profile.region, "South")
        self.assertEqual(profile.country, "India")
        self.assertEqual(profile.pincode, "560001")
        self.assertEqual(profile.telephone, "123456")
        self.assertEqual(profile.phone, "987654")
        self.assertEqual(profile.linkedin, "https://linkedin.com/company/flipkart")
        self.assertEqual(profile.tags, "ecommerce,retail,tech")
        self.assertEqual(profile.listing_status, "Unlisted")
        self.assertEqual(profile.additional_info, "Some extra notes here")
        self.assertEqual(profile.short_name, "FK")
        self.assertEqual(profile.previous_name, "Flipkart Online")
        self.assertEqual(profile.business_description, "Online ecommerce store in India.")
        self.assertEqual(profile.transacted_status, "Active")
        self.assertEqual(profile.incorp_year, 2007)
        self.assertEqual(profile.company_status, "Active")
        self.assertEqual(profile.address, "Building 8")
        self.assertEqual(profile.address_line2, "Tech Park")
        self.assertEqual(profile.contact_name, "Kalyan")
        self.assertEqual(profile.contact_designation, "CEO")
        self.assertEqual(profile.auditor_name, "PwC")
        self.assertEqual(profile.shp_year, 2023)
        self.assertEqual(profile.shp_promoter, 15.5)
        self.assertEqual(profile.shp_non_promoter, 84.5)
        self.assertTrue(profile.is_xbrl)

        # 2. Assert child table counts & values
        # Executives
        self.assertEqual(profile.executives.count(), 2)
        exec_mgmt = profile.executives.get(role_type='management')
        self.assertEqual(exec_mgmt.name, "Kalyan Krishnamurthy")
        self.assertEqual(exec_mgmt.designation, "CEO")
        self.assertEqual(exec_mgmt.belongs_to_firm_name, "Flipkart")

        exec_board = profile.executives.get(role_type='board')
        self.assertEqual(exec_board.name, "Sachin Bansal")
        self.assertEqual(exec_board.designation, "Director")
        self.assertEqual(exec_board.belongs_to_firm_name, "Flipkart Board")

        # PE Investments
        self.assertEqual(profile.pe_investments.count(), 1)
        pe_inv = profile.pe_investments.first()
        self.assertEqual(pe_inv.round, "Series H")
        self.assertEqual(pe_inv.amount, "3600")
        self.assertEqual(pe_inv.investors, ["GIC", "CPPIB", "SoftBank"])

        # Angel Investments
        self.assertEqual(profile.angel_investments.count(), 1)
        angel = profile.angel_investments.first()
        self.assertEqual(angel.date, "2009-01-01")
        self.assertEqual(angel.investors, ["Angel Investor A"])
        self.assertTrue(angel.is_exited)

        # Incubation Investments
        self.assertEqual(profile.incubation_investments.count(), 1)
        inc = profile.incubation_investments.first()
        self.assertEqual(inc.date, "2008-01-01")
        self.assertEqual(inc.incubator, "Accelerator X")

        # PE Exits
        self.assertEqual(profile.pe_exits.count(), 1)
        pe_ex = profile.pe_exits.first()
        self.assertEqual(pe_ex.deal_type, "Secondary Sale")
        self.assertEqual(pe_ex.amount, "16000")

        # PE IPOs
        self.assertEqual(profile.pe_ipos.count(), 1)
        ipo = profile.pe_ipos.first()
        self.assertEqual(ipo.date, "2025-12-01")
        self.assertEqual(ipo.ipo_size, "5000")

        # Mergers & Acquisitions
        self.assertEqual(profile.mergers_acquisitions.count(), 1)
        ma_rec = profile.mergers_acquisitions.first()
        self.assertEqual(ma_rec.company, "Myntra")
        self.assertEqual(ma_rec.amount, "300")

        # EPFO Data
        self.assertEqual(profile.epfo_data.count(), 1)
        epfo = profile.epfo_data.first()
        self.assertEqual(epfo.qrtr, "2023-Q1")
        self.assertEqual(epfo.employees, 15000)

        # Similar Companies
        self.assertEqual(profile.similar_companies.count(), 1)
        sim = profile.similar_companies.first()
        self.assertEqual(sim.name, "Amazon India")
        self.assertEqual(sim.cin, "U51909KA2011FTC060489")
        self.assertEqual(sim.total_funding, "5000")

        # 3. Assert RAG DocumentChunks created
        chunks = DocumentChunk.objects.filter(source_type='extracted_source', source_id=f"vi_{profile.id}")
        self.assertTrue(chunks.exists())
        all_chunks_text = "\n".join(chunk.content for chunk in chunks)
        self.assertIn("Flipkart", all_chunks_text)
        self.assertIn("Kalyan Krishnamurthy", all_chunks_text)
        self.assertIn("Sachin Bansal", all_chunks_text)



class VentureIntelligenceViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="testpassword")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.deal = Deal.objects.create(
            title="Flipkart",
            industry="Old Industry",
            sector="Old Sector",
            city="Old City"
        )

    @patch("deals.services.venture_intelligence.VentureIntelligenceService.fetch_company_details")
    def test_preview_view_success(self, mock_fetch):
        mock_fetch.return_value = {"success": True, "results": {"profile": {"name": "Flipkart", "cin": "U74999KA2012PTC066107"}}}
        
        response = self.client.post(
            reverse("vi-preview"),
            {"company_name": "Flipkart", "cin": "U74999KA2012PTC066107"},
            format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["success"])
        self.assertEqual(response.data["resolved_cin"], "U74999KA2012PTC066107")

    @patch("deals.services.venture_intelligence.VentureIntelligenceService.resolve_cin_candidates_via_ai")
    @patch("deals.services.venture_intelligence.VentureIntelligenceService.fetch_company_details")
    def test_preview_view_resolves_cin(self, mock_fetch, mock_resolve_cin_candidates):
        mock_resolve_cin_candidates.return_value = [{
            "cin": "U74999KA2012PTC066107",
            "entity_name": "Flipkart Private Limited",
            "confidence": 0.95,
            "is_valid": True,
        }]
        mock_fetch.return_value = {"success": True, "results": {"profile": {"name": "Flipkart"}}}
        
        response = self.client.post(
            reverse("vi-preview"),
            {"company_name": "Flipkart"},
            format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["resolved_cin"], "U74999KA2012PTC066107")

    def test_preview_view_bad_request(self):
        response = self.client.post(
            reverse("vi-preview"),
            {},
            format="json"
        )
        self.assertEqual(response.status_code, 400)

    @patch("deals.services.venture_intelligence.VentureIntelligenceService.enrich_deal")
    def test_enrich_view_success(self, mock_enrich):
        profile = VentureIntelligenceCompanyProfile.objects.create(
            cin="U74999KA2012PTC066107",
            name="Flipkart"
        )
        mock_enrich.return_value = profile
        
        response = self.client.post(
            reverse("deal-enrich", kwargs={"pk": self.deal.id}),
            {"company_name": "Flipkart", "cin": "U74999KA2012PTC066107", "relation_type": "target"},
            format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "success")
        self.assertEqual(response.data["profile"]["cin"], "U74999KA2012PTC066107")

    def test_enrich_view_unauthenticated(self):
        self.client.force_authenticate(user=None)
        response = self.client.post(
            reverse("deal-enrich", kwargs={"pk": self.deal.id}),
            {"company_name": "Flipkart"},
            format="json"
        )
        self.assertEqual(response.status_code, 401)
