from django.test import TestCase, override_settings
from unittest.mock import MagicMock, patch

from ai_orchestrator.models import AIAuditLog
from ai_orchestrator.services.ai_processor import AIProcessorService
from ai_orchestrator.services.document_processor import DocumentProcessorService
from ai_orchestrator.services.embedding_processor import EmbeddingService
from ai_orchestrator.services.parsers import ResponseParserService
from ai_orchestrator.services.flow_config import UniversalChatFlowService
from ai_orchestrator.services.universal_chat import UniversalChatService


class ResponseParserServiceTests(TestCase):
    def test_parse_standard_response_salvages_truncated_repeated_extraction_payload(self):
        raw_response = (
            '{'
            '"deal_model_data":{"title":"Alva Brothers Entertainment Ltd","industry":"Media & Entertainment",'
            '"sector":"Content Production & Distribution","funding_ask":900.0,"funding_ask_for":"Expansion",'
            '"priority":"Medium","city":"Gurgaon","themes":["Content Licensing","Regional Content"]},'
            '"metadata":{"ambiguous_points":["Losses persist"],"sources_cited":["ABE, June 2011.pdf"]},'
            '"analyst_report":"# Executive Summary\\nShort report",'
            '"deal_model_data":{"title":"Alva Brothers Entertainment Ltd"},'
            '"metadata":{"ambiguous_points":["Losses persist"]},'
            '"analyst_report":"# Executive Summary\\n**FO'
        )

        parsed_json, success, _, _ = ResponseParserService.parse_standard_response(
            raw_response,
            "",
            is_extraction_skill=True,
        )

        self.assertFalse(success)
        self.assertTrue(parsed_json.get("_salvaged"))
        self.assertEqual(parsed_json["deal_model_data"]["title"], "Alva Brothers Entertainment Ltd")
        self.assertEqual(parsed_json["deal_model_data"]["funding_ask"], "900.0")
        self.assertEqual(parsed_json["deal_model_data"]["themes"], ["Content Licensing", "Regional Content"])
        self.assertEqual(parsed_json["metadata"]["ambiguous_points"], ["Losses persist"])
        self.assertEqual(parsed_json["metadata"]["parse_mode"], "salvaged")

    def test_parse_standard_response_marks_clean_json_without_warning(self):
        raw_response = (
            '{"deal_model_data":{"title":"Acme","funding_ask":"125","themes":["B2B SaaS"]},'
            '"metadata":{"ambiguous_points":["One gap"],"sources_cited":["deck.pdf"]},'
            '"analyst_report":"# Summary"}'
        )

        parsed_json, success, _, _ = ResponseParserService.parse_standard_response(
            raw_response,
            "",
            is_extraction_skill=True,
        )

        self.assertTrue(success)
        self.assertEqual(parsed_json["deal_model_data"]["funding_ask"], "125")
        self.assertNotIn("parse_warning", parsed_json["metadata"])

    def test_salvage_extraction_payload_returns_none_without_structured_sections(self):
        salvaged = ResponseParserService.salvage_extraction_payload(
            '# Executive Summary\nNo JSON here',
            clean_response='# Executive Summary\nNo JSON here',
            thinking='',
        )
        self.assertIsNone(salvaged)

    @patch("ai_orchestrator.services.ai_processor.broadcast_audit_log_update")
    def test_standard_response_marks_salvaged_extraction_as_completed(self, _broadcast):
        audit_log = AIAuditLog.objects.create(
            source_type="onedrive_folder",
            source_id="folder-1",
            context_label="Selection Analysis",
            status="PROCESSING",
            is_success=False,
            system_prompt="system",
            user_prompt="user",
        )

        service = AIProcessorService()
        service.provider = MagicMock()
        service.provider.execute_standard.return_value = {
            "response": (
                '{"deal_model_data":{"title":"Acme","funding_ask":"125"},'
                '"metadata":{"ambiguous_points":["One gap"]},'
                '"analyst_report":"# Summary",'
                '"deal_model_data":{"title":"Acme"}'
            ),
            "thinking": "",
        }

        parsed = service._standard_response({"model": "test"}, audit_log)

        audit_log.refresh_from_db()
        self.assertTrue(audit_log.is_success)
        self.assertEqual(audit_log.status, "COMPLETED")
        self.assertIsNone(audit_log.error_message)
        self.assertTrue(parsed.get("_salvaged"))


class UniversalChatServiceTests(TestCase):
    def setUp(self):
        self.ai_service = MagicMock()
        self.service = UniversalChatService(
            self.ai_service,
            flow_config=UniversalChatFlowService.build_default_config(),
            flow_version=None,
        )

    def test_follow_up_clarification_skips_query_builder(self):
        with patch.object(self.service, "_build_query_plan") as build_query_plan:
            metadata = self.service.process_intent_and_build_metadata(
                user_message="explain that more",
                conversation_id="conv-1",
                history_context="USER: Tell me about Acme\nASSISTANT: Acme has strong margins.\n",
                audit_log_id="audit-1",
            )

        build_query_plan.assert_not_called()
        self.assertFalse(metadata["used_query_builder"])
        self.assertEqual(metadata["gate_mode"], "conversation_only")
        self.assertIn("recent conversation only", metadata["context_data"])

    def test_existing_conversation_retrieval_request_uses_query_builder(self):
        expected_plan = {
            "query_type": "comparison",
            "deal_filters": {},
            "exact_terms": [],
            "keywords": ["compare", "fintech"],
            "metric_terms": [],
            "rag_queries": ["compare this with other fintech deals"],
            "needs_stats": False,
            "deal_limit": 10,
            "chunks_per_deal": 4,
            "user_query": "compare this with other fintech deals",
        }
        with patch.object(self.service, "_build_query_plan", return_value=expected_plan) as build_query_plan, \
             patch.object(self.service, "_get_candidate_deals", return_value=[]), \
             patch.object(self.service, "_search_ranked_chunks", return_value=([], 0)):
            metadata = self.service.process_intent_and_build_metadata(
                user_message="compare this with other fintech deals",
                conversation_id="conv-1",
                history_context="USER: Tell me about Acme\nASSISTANT: Acme has strong margins.\n",
                audit_log_id="audit-1",
            )

        build_query_plan.assert_called_once()
        self.assertTrue(metadata["used_query_builder"])
        self.assertEqual(metadata["gate_mode"], "fresh_retrieval")
        self.assertEqual(metadata["query_plan"], expected_plan)
        self.assertEqual(metadata["deals_considered"], 0)
        self.assertEqual(metadata["selected_chunk_count"], 0)

    def test_first_turn_without_assistant_history_uses_query_builder(self):
        expected_plan = {
            "query_type": "pipeline_search",
            "deal_filters": {},
            "exact_terms": [],
            "keywords": ["acme"],
            "metric_terms": [],
            "rag_queries": ["Tell me about Acme"],
            "needs_stats": False,
            "deal_limit": 10,
            "chunks_per_deal": 4,
            "user_query": "Tell me about Acme",
        }
        with patch.object(self.service, "_build_query_plan", return_value=expected_plan) as build_query_plan, \
             patch.object(self.service, "_get_candidate_deals", return_value=[]), \
             patch.object(self.service, "_search_ranked_chunks", return_value=([], 0)):
            metadata = self.service.process_intent_and_build_metadata(
                user_message="Tell me about Acme",
                conversation_id="conv-1",
                history_context="",
                audit_log_id="audit-1",
            )

        build_query_plan.assert_called_once()
        self.assertTrue(metadata["used_query_builder"])
        self.assertIn("No prior assistant context", metadata["gate_reason"])

    @patch("ai_orchestrator.services.universal_chat.AIRuntimeService.get_planner_model", return_value="planner-model")
    def test_query_planner_uses_planner_model_override(self, _planner_model):
        self.ai_service.process_content.return_value = {
            "query_type": "pipeline_search",
            "deal_filters": {},
            "rag_queries": ["Tell me about Acme"],
        }

        self.service._build_query_plan("Tell me about Acme", "conv-1")

        _, kwargs = self.ai_service.process_content.call_args
        self.assertEqual(kwargs["model_override"], "planner-model")

    def test_default_flow_config_uses_deeper_retrieval_defaults(self):
        config = UniversalChatFlowService.build_default_config()
        planner = next(stage for stage in config["stages"] if stage["id"] == "query_planner")
        filtering = next(stage for stage in config["stages"] if stage["id"] == "deal_filtering")
        retrieval = next(stage for stage in config["stages"] if stage["id"] == "chunk_retrieval")
        assembly = next(stage for stage in config["stages"] if stage["id"] == "context_assembly")

        self.assertEqual(planner["settings"]["default_deal_limit"], 20)
        self.assertEqual(planner["settings"]["default_chunks_per_deal"], 8)
        self.assertEqual(planner["settings"]["max_deal_limit"], 30)
        self.assertEqual(planner["settings"]["max_chunks_per_deal"], 12)
        self.assertEqual(filtering["settings"]["candidate_pool_limit"], 250)
        self.assertEqual(retrieval["settings"]["vector_limit"], 300)
        self.assertEqual(assembly["settings"]["max_total_chunks"], 80)

    def test_normalize_plan_uses_configurable_high_caps(self):
        normalized = self.service._normalize_plan(
            {
                "query_type": "pipeline_search",
                "deal_filters": {},
                "deal_limit": 28,
                "chunks_per_deal": 11,
                "rag_queries": ["deep retrieval query"],
            },
            "deep retrieval query",
        )

        self.assertEqual(normalized["deal_limit"], 28)
        self.assertEqual(normalized["chunks_per_deal"], 11)

    def test_compute_chunk_budgets_boosts_when_one_deal_matches(self):
        one_deal = [MagicMock(id="deal-1")]

        max_per_deal, max_total = self.service._compute_chunk_budgets(
            {
                "chunks_per_deal": 8,
            },
            one_deal,
        )

        self.assertEqual(max_per_deal, 20)
        self.assertEqual(max_total, 60)

    def test_candidate_deals_prefers_semantic_profile_hits(self):
        semantic_deal = MagicMock()
        semantic_deal.id = "deal-semantic"
        semantic_deal.created_at = 1

        with patch.object(self.service.embed_service, "search_deal_profiles", return_value=[semantic_deal]), \
             patch("ai_orchestrator.services.universal_chat.Deal.objects") as deal_manager:
            queryset = MagicMock()
            queryset.prefetch_related.return_value = queryset
            queryset.order_by.return_value = []
            deal_manager.all.return_value = queryset

            deals = self.service._get_candidate_deals(
                {
                    "deal_filters": {},
                    "exact_terms": [],
                    "keywords": [],
                    "metric_terms": [],
                    "rag_queries": ["collections quality in Karnataka"],
                    "user_query": "collections quality in Karnataka",
                    "deal_limit": 5,
                }
            )

        self.assertEqual(deals, [semantic_deal])

    def test_simulate_query_returns_retrieval_diagnostics(self):
        deal = MagicMock()

        with patch.object(self.service, "_build_query_plan", return_value={
            "query_type": "pipeline_search",
            "deal_filters": {},
            "exact_terms": [],
            "keywords": ["acme"],
            "metric_terms": [],
            "rag_queries": ["Tell me about Acme"],
            "needs_stats": False,
            "deal_limit": 20,
            "chunks_per_deal": 8,
            "user_query": "Tell me about Acme",
        }), patch.object(self.service, "_get_candidate_deals", return_value=[deal]), patch.object(
            self.service,
            "_search_ranked_chunks",
            return_value=(
                [],
                {
                    "candidate_chunk_count": 120,
                    "selected_chunk_count": 0,
                    "selected_chunk_count_by_deal": {},
                    "effective_chunks_per_deal": 20,
                    "max_total_chunks": 60,
                    "dropped_by_per_deal_cap": 0,
                    "dropped_by_total_cap": 12,
                    "dropped_as_duplicates": 0,
                    "dropped_by_zero_score": 8,
                },
            ),
        ), patch.object(self.service, "_serialize_deal", return_value={"title": "Acme"}):
            simulation = self.service.simulate_query("Tell me about Acme")

        self.assertEqual(simulation["retrieval_diagnostics"]["planner_requested_deal_limit"], 20)
        self.assertEqual(simulation["retrieval_diagnostics"]["effective_chunks_per_deal"], 20)
        self.assertEqual(simulation["retrieval_diagnostics"]["candidate_chunk_count"], 120)


class EmbeddingServiceTests(TestCase):
    def test_rerank_chunks_falls_back_to_heuristics_when_reranker_disabled(self):
        service = EmbeddingService()
        service.reranker_model = ""
        chunk_a = MagicMock()
        chunk_a.source_id = "a"
        chunk_a.metadata = {"chunk_kind": "metric", "chunk_index": 0}
        chunk_a.distance = 0.3
        chunk_b = MagicMock()
        chunk_b.source_id = "b"
        chunk_b.metadata = {"chunk_kind": "risk", "chunk_index": 0}
        chunk_b.distance = 0.2

        ranked = service._rerank_chunks([chunk_a, chunk_b], "ARR by quarter", limit=2)

        self.assertEqual(ranked[0], chunk_a)

    @override_settings(RERANKER_MODEL="bge-reranker")
    def test_model_rerank_chunks_uses_provider_scores(self):
        service = EmbeddingService()
        service.reranker_model = "bge-reranker"
        service.reranker = MagicMock()
        service.reranker.rerank.return_value = [
            {"index": 1, "score": 0.95},
            {"index": 0, "score": 0.7},
        ]
        chunk_a = MagicMock()
        chunk_a.content = "first"
        chunk_a.source_id = "a"
        chunk_a.metadata = {"chunk_kind": "claim", "chunk_index": 0}
        chunk_b = MagicMock()
        chunk_b.content = "second"
        chunk_b.source_id = "b"
        chunk_b.metadata = {"chunk_kind": "risk", "chunk_index": 0}

        ranked = service._rerank_chunks([chunk_a, chunk_b], "Which risk matters most?", limit=2)

        self.assertEqual(ranked[0], chunk_b)
        self.assertEqual(getattr(chunk_b, "rerank_score", None), 0.95)


class DocumentProcessorServiceTests(TestCase):
    @override_settings(
        DOC_PROCESSOR_URL="http://docproc.internal",
        DOC_PROCESSOR_API_KEY="secret-token",
        DOC_PROCESSOR_TIMEOUT=123,
    )
    @patch("ai_orchestrator.services.document_processor.requests.post")
    def test_remote_extraction_result_is_normalized(self, mock_post):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "raw_extracted_text": "Raw OCR text",
            "normalized_text": "Normalized OCR text",
            "extraction_mode": "docproc_remote",
            "transcription_status": "complete",
            "quality_flags": ["vision_first"],
            "render_metadata": {"route": "vision_first", "page_count": 2},
        }
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response

        service = DocumentProcessorService()
        result = service.get_extraction_result(b"file-bytes", "example.pdf", page_limit=5)

        self.assertEqual(result["text"], "Normalized OCR text")
        self.assertEqual(result["raw_extracted_text"], "Raw OCR text")
        self.assertEqual(result["normalized_text"], "Normalized OCR text")
        self.assertEqual(result["mode"], "docproc_remote")
        self.assertEqual(result["quality_flags"], ["vision_first"])
        self.assertEqual(result["render_metadata"]["page_count"], 2)

        _, kwargs = mock_post.call_args
        self.assertEqual(kwargs["timeout"], 123)
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer secret-token")
        self.assertEqual(kwargs["json"]["filename"], "example.pdf")
        self.assertEqual(kwargs["json"]["page_limit"], 5)

    @override_settings(DOC_PROCESSOR_URL="http://docproc.internal")
    @patch.object(DocumentProcessorService, "_local_extract")
    @patch("ai_orchestrator.services.document_processor.requests.post")
    def test_remote_failure_falls_back_to_local_extraction(self, mock_post, mock_local_extract):
        mock_post.side_effect = RuntimeError("docproc unavailable")
        mock_local_extract.return_value = {
            "text": "Local text",
            "raw_extracted_text": "Local text",
            "normalized_text": "Local text",
            "mode": "fallback_text",
            "transcription_status": "complete",
            "quality_flags": ["local_backend_fallback"],
            "render_metadata": {},
        }

        service = DocumentProcessorService()
        result = service.get_extraction_result(b"file-bytes", "example.docx")

        self.assertEqual(result["mode"], "fallback_text")
        self.assertEqual(result["normalized_text"], "Local text")
        mock_local_extract.assert_called_once()
