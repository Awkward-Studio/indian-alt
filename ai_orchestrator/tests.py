from django.test import TestCase, override_settings
from unittest.mock import MagicMock, patch

from deals.models import Deal
from ai_orchestrator.models import AIAuditLog
from ai_orchestrator.services.ai_processor import AIProcessorService
from ai_orchestrator.services.document_processor import DocumentProcessorService
from ai_orchestrator.services.embedding_processor import EmbeddingService
from ai_orchestrator.services.parsers import ResponseParserService
from ai_orchestrator.services.flow_config import (
    DEFAULT_ANSWER_PROMPT,
    DEFAULT_PLANNER_PROMPT,
    UniversalChatFlowService,
)
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

    def test_parse_stream_routes_qwen_think_tags_to_thinking(self):
        stream = [
            '{"response": "<think>internal plan</think><response>final answer", "done": false}',
            '{"response": "</response>", "done": true}',
        ]

        chunks = list(ResponseParserService.parse_stream(stream))
        thinking = "".join(item[1] for item in chunks)
        response = "".join(item[2] for item in chunks)

        self.assertEqual(thinking, "internal plan")
        self.assertEqual(response, "final answer")

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
            "hard_filters": {},
            "exact_terms": [],
            "semantic_queries": ["compare this with other fintech deals"],
            "soft_constraints": [],
            "metric_terms": [],
            "evidence_preference": "mixed",
            "result_shape": "cross_pipeline",
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
            "hard_filters": {},
            "exact_terms": [],
            "semantic_queries": ["Tell me about Acme"],
            "soft_constraints": [],
            "metric_terms": [],
            "evidence_preference": "mixed",
            "result_shape": "shortlist",
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
            "hard_filters": {},
            "semantic_queries": ["Tell me about Acme"],
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
        self.assertEqual(planner["settings"]["default_chunks_per_deal"], 12)
        self.assertEqual(planner["settings"]["max_deal_limit"], 30)
        self.assertEqual(planner["settings"]["max_chunks_per_deal"], 20)
        self.assertEqual(filtering["settings"]["candidate_pool_limit"], 250)
        self.assertEqual(retrieval["settings"]["vector_limit"], 500)
        self.assertEqual(assembly["settings"]["max_total_chunks"], 120)

    def test_validate_config_merges_new_default_stage_settings_into_older_configs(self):
        validated = UniversalChatFlowService.validate_config(
            {
                "stages": [
                    {
                        "id": "query_planner",
                        "enabled": True,
                        "settings": {
                            "prompt_template": DEFAULT_PLANNER_PROMPT,
                            "fallback_query_type": "pipeline_search",
                        },
                    },
                    {
                        "id": "deal_filtering",
                        "enabled": True,
                        "settings": {},
                    },
                    {
                        "id": "chunk_retrieval",
                        "enabled": True,
                        "settings": {
                            "vector_limit": 300,
                        },
                    },
                    {
                        "id": "chunk_rerank",
                        "enabled": True,
                        "settings": {},
                    },
                    {
                        "id": "context_assembly",
                        "enabled": True,
                        "settings": {},
                    },
                    {
                        "id": "answer_generation",
                        "enabled": True,
                        "settings": {
                            "prompt_template": DEFAULT_ANSWER_PROMPT,
                        },
                    },
                ]
            }
        )

        planner = next(stage for stage in validated["stages"] if stage["id"] == "query_planner")
        retrieval = next(stage for stage in validated["stages"] if stage["id"] == "chunk_retrieval")
        assembly = next(stage for stage in validated["stages"] if stage["id"] == "context_assembly")

        self.assertEqual(planner["settings"]["default_chunks_per_deal"], 12)
        self.assertEqual(retrieval["settings"]["vector_limit"], 300)
        self.assertEqual(retrieval["settings"]["fallback_candidate_limit"], 600)
        self.assertEqual(assembly["settings"]["max_total_chunks"], 120)

    def test_normalize_plan_accepts_new_schema(self):
        normalized = self.service._normalize_plan(
            {
                "query_type": "pipeline_search",
                "hard_filters": {"industry": "Fintech"},
                "named_entities": [{"type": "deal", "text": "Acme", "confidence": 0.95}],
                "semantic_queries": ["deep retrieval query"],
                "soft_constraints": ["prefer companies with clear monetization"],
                "metric_terms": ["ARR"],
                "evidence_preference": "metrics",
                "result_shape": "shortlist",
                "selection_mode": "balanced",
                "stats_mode": "none",
                "needs_stats": False,
                "deal_limit": 28,
                "chunks_per_deal": 11,
                "global_chunk_limit": 18,
            },
            "deep retrieval query",
        )

        self.assertEqual(normalized["hard_filters"]["industry"], "Fintech")
        self.assertEqual(normalized["named_entities"][0]["text"], "Acme")
        self.assertEqual(normalized["semantic_queries"], ["deep retrieval query"])
        self.assertEqual(normalized["soft_constraints"], ["prefer companies with clear monetization"])
        self.assertEqual(normalized["evidence_preference"], "metrics")
        self.assertEqual(normalized["result_shape"], "shortlist")
        self.assertEqual(normalized["selection_mode"], "balanced")
        self.assertEqual(normalized["stats_mode"], "none")
        self.assertEqual(normalized["deal_limit"], 28)
        self.assertEqual(normalized["chunks_per_deal"], 11)
        self.assertEqual(normalized["global_chunk_limit"], 18)

    def test_normalize_plan_translates_old_schema_during_compatibility_window(self):
        normalized = self.service._normalize_plan(
            {
                "query_type": "exact_lookup",
                "deal_filters": {"title": "Acme"},
                "rag_queries": ["Tell me about Acme"],
                "metric_terms": ["revenue"],
            },
            "Tell me about Acme",
        )

        self.assertEqual(normalized["hard_filters"]["title"], "Acme")
        self.assertEqual(normalized["semantic_queries"], ["Tell me about Acme"])
        self.assertEqual(normalized["result_shape"], "single_deal")
        self.assertEqual(normalized["named_entities"], [])

    def test_normalize_plan_falls_back_to_minimal_defaults_for_malformed_output(self):
        normalized = self.service._normalize_plan(
            {
                "query_type": "not-real",
                "hard_filters": "bad-shape",
                "semantic_queries": None,
                "evidence_preference": "unknown",
                "result_shape": "invalid",
            },
            "Broad market map",
        )

        self.assertEqual(normalized["query_type"], "pipeline_search")
        self.assertEqual(normalized["hard_filters"], {})
        self.assertEqual(normalized["semantic_queries"], ["Broad market map"])
        self.assertEqual(normalized["evidence_preference"], "mixed")
        self.assertEqual(normalized["result_shape"], "shortlist")
        self.assertEqual(normalized["named_entities"], [])
        self.assertEqual(normalized["selection_mode"], "balanced")
        self.assertEqual(normalized["stats_mode"], "none")

    def test_normalize_plan_defaults_named_set_to_depth_first(self):
        normalized = self.service._normalize_plan(
            {
                "query_type": "comparison",
                "hard_filters": {},
                "named_entities": [
                    {"type": "deal", "text": "Company A", "confidence": 0.9},
                    {"type": "deal", "text": "Company B", "confidence": 0.9},
                ],
                "semantic_queries": ["compare Company A and Company B"],
                "soft_constraints": [],
                "metric_terms": [],
                "evidence_preference": "mixed",
                "result_shape": "named_set",
                "needs_stats": False,
            },
            "compare Company A and Company B",
        )

        self.assertEqual(normalized["query_type"], "comparison")
        self.assertEqual(normalized["result_shape"], "named_set")
        self.assertEqual(normalized["selection_mode"], "depth_first")
        self.assertEqual(normalized["deal_limit"], 2)

    def test_normalize_plan_preserves_document_focused_single_deal_plan(self):
        normalized = self.service._normalize_plan(
            {
                "query_type": "exact_lookup",
                "hard_filters": {},
                "named_entities": [{"type": "deal", "text": "Named Deal", "confidence": 0.98}],
                "semantic_queries": ["Named Deal annual report", "Named Deal financial statements"],
                "soft_constraints": [],
                "metric_terms": [],
                "evidence_preference": "documents",
                "result_shape": "single_deal",
                "selection_mode": "depth_first",
                "needs_stats": False,
                "stats_mode": "none",
            },
            "go through the annual reports for a named company",
        )

        self.assertEqual(normalized["query_type"], "exact_lookup")
        self.assertEqual(normalized["named_entities"][0]["text"], "Named Deal")
        self.assertEqual(normalized["result_shape"], "single_deal")
        self.assertEqual(normalized["evidence_preference"], "documents")
        self.assertEqual(normalized["deal_limit"], 1)
        self.assertIn("annual report", normalized["semantic_queries"][0].lower())

    def test_normalize_plan_promotes_single_named_deal_queries_to_depth_first_single_deal(self):
        normalized = self.service._normalize_plan(
            {
                "query_type": "narrative",
                "hard_filters": {},
                "named_entities": [{"type": "deal", "text": "Named Deal", "confidence": 0.9}],
                "semantic_queries": ["Tell me everything about this company"],
                "soft_constraints": [],
                "metric_terms": [],
                "evidence_preference": "mixed",
                "result_shape": "shortlist",
                "selection_mode": "balanced",
                "stats_mode": "none",
                "needs_stats": False,
                "deal_limit": 8,
                "chunks_per_deal": 2,
                "global_chunk_limit": 8,
            },
            "Tell me everything about Named Deal",
        )

        self.assertEqual(normalized["result_shape"], "single_deal")
        self.assertEqual(normalized["selection_mode"], "depth_first")
        self.assertEqual(normalized["deal_limit"], 1)
        self.assertGreaterEqual(normalized["chunks_per_deal"], 8)
        self.assertGreaterEqual(normalized["global_chunk_limit"], 24)

    def test_promote_plan_from_resolved_named_deals_collapses_aliases_to_single_deal_scope(self):
        plan = {
            "query_type": "narrative",
            "result_shape": "shortlist",
            "selection_mode": "balanced",
            "stats_mode": "none",
            "deal_limit": 8,
            "chunks_per_deal": 2,
            "global_chunk_limit": 8,
        }

        resolved = [MagicMock(id="deal-1")]

        self.service._promote_plan_from_resolved_named_deals(plan, resolved)

        self.assertEqual(plan["result_shape"], "single_deal")
        self.assertEqual(plan["selection_mode"], "depth_first")
        self.assertEqual(plan["deal_limit"], 1)
        self.assertGreaterEqual(plan["chunks_per_deal"], 12)
        self.assertGreaterEqual(plan["global_chunk_limit"], 24)

    def test_promote_plan_from_resolved_named_deals_promotes_small_named_sets_to_depth_first(self):
        plan = {
            "query_type": "comparison",
            "result_shape": "shortlist",
            "selection_mode": "balanced",
            "stats_mode": "none",
            "deal_limit": 8,
            "chunks_per_deal": 2,
            "global_chunk_limit": 8,
        }

        resolved = [MagicMock(id="deal-1"), MagicMock(id="deal-2")]

        self.service._promote_plan_from_resolved_named_deals(plan, resolved)

        self.assertEqual(plan["result_shape"], "named_set")
        self.assertEqual(plan["selection_mode"], "depth_first")
        self.assertEqual(plan["deal_limit"], 2)
        self.assertGreaterEqual(plan["chunks_per_deal"], 6)
        self.assertGreaterEqual(plan["global_chunk_limit"], 12)

    def test_compute_chunk_budgets_boosts_when_one_deal_matches(self):
        one_deal = [MagicMock(id="deal-1")]

        max_per_deal, max_total = self.service._compute_chunk_budgets(
            {
                "chunks_per_deal": 8,
            },
            one_deal,
        )

        self.assertEqual(max_per_deal, 24)
        self.assertEqual(max_total, 90)

    def test_compute_chunk_budgets_expands_for_single_deal_depth_first_queries(self):
        one_deal = [MagicMock(id="deal-1")]

        max_per_deal, max_total = self.service._compute_chunk_budgets(
            {
                "chunks_per_deal": 8,
                "selection_mode": "depth_first",
                "result_shape": "single_deal",
                "global_chunk_limit": 24,
            },
            one_deal,
        )

        self.assertGreaterEqual(max_per_deal, 8)
        self.assertEqual(max_total, 24)

    def test_candidate_deals_prefers_semantic_profile_hits(self):
        semantic_deal = MagicMock()
        semantic_deal.id = "deal-semantic"
        semantic_deal.created_at = 1

        with patch.object(self.service.embed_service, "search_deal_profiles", return_value=[semantic_deal]), \
             patch("ai_orchestrator.services.universal_chat.Deal.objects") as deal_manager:
            queryset = MagicMock()
            queryset.select_related.return_value = queryset
            queryset.prefetch_related.return_value = queryset
            queryset.filter.return_value = queryset
            queryset.distinct.return_value = queryset
            queryset.order_by.return_value = []
            deal_manager.all.return_value = queryset

            deals = self.service._get_candidate_deals(
                {
                    "hard_filters": {},
                    "exact_terms": [],
                    "semantic_queries": ["collections quality in Karnataka"],
                    "soft_constraints": [],
                    "metric_terms": [],
                    "evidence_preference": "mixed",
                    "result_shape": "shortlist",
                    "user_query": "collections quality in Karnataka",
                    "deal_limit": 5,
                }
            )

        self.assertEqual(deals, [semantic_deal])

    def test_candidate_deals_exact_title_match_beats_generic_semantic(self):
        exact_deal = MagicMock(spec=Deal)
        exact_deal.id = "deal-mm"
        exact_deal.title = "Man Matters - Series C / Growth Round"
        exact_deal.industry = "D2C Wellness / Personal Care"
        exact_deal.sector = "Consumer"
        exact_deal.city = "Mumbai"
        exact_deal.deal_summary = "Funding ask INR 50 Cr with strong repeat customer behavior."
        exact_deal.funding_ask = "INR 50 Cr"
        exact_deal.funding_ask_for = "Growth"
        exact_deal.themes = ["Consumer", "Wellness"]
        exact_deal.created_at = 2
        exact_deal.phase_logs = MagicMock()
        exact_deal.phase_logs.all.return_value.order_by.return_value = []
        exact_deal.retrieval_profile = MagicMock(profile_text="Man Matters repeat customer behavior and funding ask")

        generic_deal = MagicMock(spec=Deal)
        generic_deal.id = "deal-generic"
        generic_deal.title = "Beauty & Wellness Sector Investment Analysis"
        generic_deal.industry = "Beauty"
        generic_deal.sector = "Consumer"
        generic_deal.city = "Delhi"
        generic_deal.deal_summary = "Broad sector report."
        generic_deal.funding_ask = ""
        generic_deal.funding_ask_for = ""
        generic_deal.themes = ["Beauty"]
        generic_deal.created_at = 1
        generic_deal.phase_logs = MagicMock()
        generic_deal.phase_logs.all.return_value.order_by.return_value = []
        generic_deal.retrieval_profile = MagicMock(profile_text="sector overview")

        with patch.object(self.service.embed_service, "search_deal_profiles", return_value=[generic_deal]), \
             patch.object(self.service, "_resolve_named_entity_deals", return_value=[exact_deal]), \
             patch.object(self.service, "_keyword_candidate_pool", return_value=[exact_deal]), \
             patch("ai_orchestrator.services.universal_chat.Deal.objects") as deal_manager:
            queryset = MagicMock()
            queryset.select_related.return_value = queryset
            queryset.prefetch_related.return_value = queryset
            queryset.filter.return_value = queryset
            queryset.distinct.return_value = queryset
            queryset.order_by.return_value = [generic_deal]
            deal_manager.all.return_value = queryset

            deals = self.service._get_candidate_deals(
                {
                    "hard_filters": {},
                    "named_entities": [{"type": "deal", "text": "Man Matters", "confidence": 0.95}],
                    "exact_terms": [],
                    "semantic_queries": ["Tell me about Man Matters funding ask and repeat behavior"],
                    "soft_constraints": [],
                    "metric_terms": ["funding ask"],
                    "evidence_preference": "summary",
                    "result_shape": "single_deal",
                    "user_query": "Tell me about Man Matters funding ask and repeat behavior",
                    "deal_limit": 5,
                }
            )

        self.assertEqual(deals[0], exact_deal)

    def test_candidate_deals_uses_multi_query_semantic_recall(self):
        first_semantic = MagicMock(spec=Deal)
        first_semantic.id = "deal-a"
        first_semantic.title = "Alpha Finance"
        first_semantic.industry = "Fintech"
        first_semantic.sector = "Lending"
        first_semantic.city = "Mumbai"
        first_semantic.deal_summary = "Lending platform."
        first_semantic.funding_ask = ""
        first_semantic.funding_ask_for = ""
        first_semantic.themes = []
        first_semantic.created_at = 2
        first_semantic.phase_logs = MagicMock()
        first_semantic.phase_logs.all.return_value.order_by.return_value = []
        first_semantic.retrieval_profile = MagicMock(profile_text="collections quality and rural borrowers")

        second_semantic = MagicMock(spec=Deal)
        second_semantic.id = "deal-b"
        second_semantic.title = "Beta Credit"
        second_semantic.industry = "Fintech"
        second_semantic.sector = "NBFC"
        second_semantic.city = "Bengaluru"
        second_semantic.deal_summary = "Collections platform."
        second_semantic.funding_ask = ""
        second_semantic.funding_ask_for = ""
        second_semantic.themes = []
        second_semantic.created_at = 1
        second_semantic.phase_logs = MagicMock()
        second_semantic.phase_logs.all.return_value.order_by.return_value = []
        second_semantic.retrieval_profile = MagicMock(profile_text="credit underwriting and field collections")

        with patch.object(self.service.embed_service, "search_deal_profiles", side_effect=[[first_semantic], [second_semantic]]), \
             patch.object(self.service, "_keyword_candidate_pool", return_value=[]), \
             patch("ai_orchestrator.services.universal_chat.Deal.objects") as deal_manager:
            queryset = MagicMock()
            queryset.select_related.return_value = queryset
            queryset.prefetch_related.return_value = queryset
            queryset.filter.return_value = queryset
            queryset.distinct.return_value = queryset
            queryset.order_by.return_value = []
            deal_manager.all.return_value = queryset

            deals = self.service._get_candidate_deals(
                {
                    "hard_filters": {"industry": "Fintech"},
                    "exact_terms": [],
                    "semantic_queries": ["collections quality", "rural borrowers"],
                    "soft_constraints": [],
                    "metric_terms": [],
                    "evidence_preference": "mixed",
                    "result_shape": "shortlist",
                    "user_query": "Find fintech collections businesses",
                    "deal_limit": 5,
                }
            )

        self.assertEqual([deal.id for deal in deals], ["deal-a", "deal-b"])

    def test_removed_domain_specific_boosts_do_not_break_exact_title_queries(self):
        exact_deal = MagicMock(spec=Deal)
        exact_deal.id = "deal-mm"
        exact_deal.title = "Man Matters - Series C / Growth Round"
        exact_deal.industry = "D2C Wellness / Personal Care"
        exact_deal.sector = "Consumer"
        exact_deal.city = "Mumbai"
        exact_deal.deal_summary = "Consumer wellness brand."
        exact_deal.funding_ask = ""
        exact_deal.funding_ask_for = ""
        exact_deal.themes = []
        exact_deal.created_at = 2
        exact_deal.phase_logs = MagicMock()
        exact_deal.phase_logs.all.return_value.order_by.return_value = []
        exact_deal.retrieval_profile = MagicMock(profile_text="profile")

        generic = MagicMock(spec=Deal)
        generic.id = "deal-generic"
        generic.title = "Sector Overview"
        generic.industry = "Consumer"
        generic.sector = "Research"
        generic.city = "Delhi"
        generic.deal_summary = "Broad report."
        generic.funding_ask = ""
        generic.funding_ask_for = ""
        generic.themes = []
        generic.created_at = 1
        generic.phase_logs = MagicMock()
        generic.phase_logs.all.return_value.order_by.return_value = []
        generic.retrieval_profile = MagicMock(profile_text="profile")

        with patch.object(self.service.embed_service, "search_deal_profiles", return_value=[generic]), \
             patch.object(self.service, "_resolve_named_entity_deals", return_value=[exact_deal]), \
             patch.object(self.service, "_keyword_candidate_pool", return_value=[exact_deal]), \
             patch("ai_orchestrator.services.universal_chat.Deal.objects") as deal_manager:
            queryset = MagicMock()
            queryset.select_related.return_value = queryset
            queryset.prefetch_related.return_value = queryset
            queryset.filter.return_value = queryset
            queryset.distinct.return_value = queryset
            queryset.order_by.return_value = [generic]
            deal_manager.all.return_value = queryset

            deals = self.service._get_candidate_deals(
                {
                    "hard_filters": {},
                    "named_entities": [{"type": "deal", "text": "Man Matters - Series C / Growth Round", "confidence": 0.98}],
                    "exact_terms": [],
                    "semantic_queries": ["company overview"],
                    "soft_constraints": [],
                    "metric_terms": [],
                    "evidence_preference": "summary",
                    "result_shape": "single_deal",
                    "user_query": "Tell me about Man Matters",
                    "deal_limit": 5,
                }
            )

        self.assertEqual(deals[0], exact_deal)

    def test_apply_result_shape_scope_prefers_resolved_named_deals_even_for_shortlist(self):
        named = MagicMock(spec=Deal)
        named.id = "deal-named"
        named.title = "Named Deal"

        unrelated = MagicMock(spec=Deal)
        unrelated.id = "deal-other"
        unrelated.title = "Other Deal"

        scoped = self.service._apply_result_shape_scope(
            [
                (90.0, unrelated),
                (80.0, named),
            ],
            {
                "result_shape": "shortlist",
                "stats_mode": "none",
                "_resolved_named_deal_ids": ["deal-named"],
            },
            result_limit=8,
        )

        self.assertEqual(scoped, [(80.0, named)])

    def test_scope_deals_for_chunk_retrieval_prefers_resolved_named_deals_even_for_shortlist(self):
        named = MagicMock(spec=Deal)
        named.id = "deal-named"
        named.title = "Named Deal"

        unrelated = MagicMock(spec=Deal)
        unrelated.id = "deal-other"
        unrelated.title = "Other Deal"

        scoped = self.service._scope_deals_for_chunk_retrieval(
            {
                "result_shape": "shortlist",
                "stats_mode": "none",
                "_resolved_named_deal_ids": ["deal-named"],
            },
            [unrelated, named],
        )

        self.assertEqual(scoped, [named])

    def test_resolve_named_entity_deals_uses_precise_db_matching_without_semantic_fallback(self):
        precise = MagicMock(spec=Deal)
        precise.id = "deal-precise"
        precise.title = "Wildcraft India Private Limited"

        queryset = MagicMock()
        queryset.filter.return_value = queryset
        queryset.order_by.return_value.first.return_value = precise

        with patch.object(self.service, "_database_exact_match_pool", side_effect=[[precise], []]), \
             patch.object(self.service, "_best_matching_field_value", return_value=None), \
             patch.object(self.service.embed_service, "search_deal_profiles") as search_deal_profiles:
            resolved = self.service._resolve_named_entity_deals(
                queryset,
                {
                    "named_entities": [
                        {"type": "deal", "text": "Wildcraft", "confidence": 0.9},
                        {"type": "deal", "text": "Wildcraft Ltd.", "confidence": 0.9},
                    ]
                },
                limit=8,
            )

        self.assertEqual(resolved, [precise])
        search_deal_profiles.assert_not_called()

    def test_tokenize_keywords_strips_generic_query_words(self):
        keywords = self.service._tokenize_keywords(
            "Which consumer or wellness deals mention a funding ask and strong repeat customer behavior?"
        )

        self.assertNotIn("Which", keywords)
        self.assertNotIn("deals", keywords)
        self.assertNotIn("mention", keywords)
        self.assertIn("consumer", [keyword.lower() for keyword in keywords])
        self.assertIn("wellness", [keyword.lower() for keyword in keywords])
        self.assertIn("repeat", [keyword.lower() for keyword in keywords])

    def test_simulate_query_returns_retrieval_diagnostics(self):
        deal = MagicMock()

        with patch.object(self.service, "_build_query_plan", return_value={
            "query_type": "pipeline_search",
            "hard_filters": {},
            "named_entities": [],
            "exact_terms": [],
            "semantic_queries": ["Tell me about Acme"],
            "soft_constraints": [],
            "metric_terms": [],
            "evidence_preference": "mixed",
            "result_shape": "shortlist",
            "selection_mode": "balanced",
            "needs_stats": False,
            "stats_mode": "none",
            "deal_limit": 20,
            "chunks_per_deal": 8,
            "global_chunk_limit": 24,
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
        self.assertEqual(simulation["retrieval_diagnostics"]["selection_mode"], "balanced")

    @patch("ai_orchestrator.services.universal_chat.AIRuntimeService.get_planner_model", return_value="planner-model")
    def test_simulate_query_can_run_analysis_and_return_answer(self, _planner_model):
        deal = MagicMock()

        with patch.object(self.service, "_build_query_plan", return_value={
            "query_type": "pipeline_search",
            "hard_filters": {},
            "named_entities": [],
            "exact_terms": [],
            "semantic_queries": ["Tell me about Acme"],
            "soft_constraints": [],
            "metric_terms": [],
            "evidence_preference": "mixed",
            "result_shape": "shortlist",
            "selection_mode": "balanced",
            "needs_stats": False,
            "stats_mode": "none",
            "deal_limit": 8,
            "chunks_per_deal": 2,
            "global_chunk_limit": 8,
            "user_query": "Tell me about Acme",
        }), patch.object(self.service, "_get_candidate_deals", return_value=[deal]), patch.object(
            self.service,
            "_search_ranked_chunks",
            return_value=([], {"candidate_chunk_count": 0, "selected_chunk_count_by_deal": {}}),
        ), patch.object(
            self.service,
            "_serialize_deal",
            return_value={"deal_id": "deal-1", "title": "Acme"},
        ), patch.object(
            self.service,
            "_format_context_data",
            return_value=("context block", {"chars_before_trim": 12, "chars_after_trim": 12, "omitted_chunk_count": 0}),
        ):
            self.service.ai_service.provider.execute_standard = MagicMock(return_value={"response": "analysis answer"})
            simulation = self.service.simulate_query(
                "Tell me about Acme",
                run_analysis=True,
                include_analysis_prompt=True,
            )

        self.assertEqual(simulation["analysis_answer"], "analysis answer")
        self.assertEqual(simulation["analysis_model_used"], "planner-model")
        self.assertEqual(simulation["analysis_input_summary"]["selected_deal_count"], 1)
        self.assertEqual(simulation["analysis_context_preview"], "context block")
        self.assertTrue(simulation["analysis_prompt_preview"])
        args, _ = self.service.ai_service.provider.execute_standard.call_args
        self.assertEqual(args[0]["model"], "planner-model")
        self.assertEqual(args[0]["prompt"], simulation["analysis_prompt_preview"])

    def test_search_ranked_chunks_prefers_deal_summary_for_broad_deal_question(self):
        deal = MagicMock()
        deal.id = "deal-1"
        deal.title = "Man Matters - Series C / Growth Round"

        summary_chunk = MagicMock()
        summary_chunk.id = "chunk-summary"
        summary_chunk.deal = deal
        summary_chunk.deal_id = deal.id
        summary_chunk.source_type = "deal_summary"
        summary_chunk.source_id = "deal-1"
        summary_chunk.metadata = {"title": deal.title, "chunk_index": 0}
        summary_chunk.content = "Funding ask is INR 50 Cr and repeat customer behavior is strong."
        summary_chunk.distance = 0.41
        summary_chunk.rerank_score = 0.6

        risk_chunk = MagicMock()
        risk_chunk.id = "chunk-risk"
        risk_chunk.deal = deal
        risk_chunk.deal_id = deal.id
        risk_chunk.source_type = "document"
        risk_chunk.source_id = "doc-1"
        risk_chunk.metadata = {"title": "call notes.docx", "chunk_kind": "risk", "chunk_index": 0}
        risk_chunk.content = "Difficulty retaining customers once foreign brands return."
        risk_chunk.distance = 0.34
        risk_chunk.rerank_score = 0.2

        plan = {
            "query_type": "pipeline_search",
            "named_entities": [],
            "exact_terms": [],
            "semantic_queries": ["Which consumer or wellness deals mention a funding ask and strong repeat customer behavior?"],
            "soft_constraints": [],
            "metric_terms": ["funding ask"],
            "evidence_preference": "summary",
            "result_shape": "shortlist",
            "selection_mode": "balanced",
            "stats_mode": "none",
            "global_chunk_limit": 10,
            "user_query": "Which consumer or wellness deals mention a funding ask and strong repeat customer behavior?",
        }

        with patch.object(self.service.embed_service, "search_global_chunks", return_value=[risk_chunk, summary_chunk]), \
             patch.object(self.service.embed_service, "_rerank_chunks", side_effect=lambda chunks, query, limit: chunks), \
             patch.object(self.service, "_augment_with_deal_summary_candidates", side_effect=lambda chunks, deals: chunks), \
             patch.object(self.service, "_compute_chunk_budgets", return_value=(10, 10)):
            selected, diagnostics = self.service._search_ranked_chunks(plan, [deal])

        self.assertEqual(selected[0]["chunk"], summary_chunk)
        self.assertEqual(diagnostics["selected_chunk_count"], 2)

    def test_search_ranked_chunks_prefers_document_backed_evidence_for_single_deal_depth_first_queries(self):
        deal = MagicMock()
        deal.id = "deal-1"
        deal.title = "Acme"

        summary_chunk = MagicMock()
        summary_chunk.id = "chunk-summary"
        summary_chunk.deal = deal
        summary_chunk.deal_id = deal.id
        summary_chunk.source_type = "deal_summary"
        summary_chunk.source_id = "deal-1"
        summary_chunk.metadata = {"title": deal.title, "chunk_index": 0}
        summary_chunk.content = "High-level summary."
        summary_chunk.distance = 0.4
        summary_chunk.rerank_score = 0.6

        document_chunk = MagicMock()
        document_chunk.id = "chunk-document"
        document_chunk.deal = deal
        document_chunk.deal_id = deal.id
        document_chunk.source_type = "extracted_source"
        document_chunk.source_id = "doc-1"
        document_chunk.metadata = {"filename": "annual_report.pdf", "chunk_kind": "normalized_text", "chunk_index": 0}
        document_chunk.content = "Detailed financial statements and management discussion."
        document_chunk.distance = 0.4
        document_chunk.rerank_score = 0.6

        plan = {
            "query_type": "narrative",
            "named_entities": [{"type": "deal", "text": "Acme", "confidence": 0.9}],
            "exact_terms": ["Acme"],
            "semantic_queries": ["Tell me all you can about Acme"],
            "soft_constraints": [],
            "metric_terms": [],
            "evidence_preference": "mixed",
            "result_shape": "single_deal",
            "selection_mode": "depth_first",
            "stats_mode": "none",
            "global_chunk_limit": 24,
            "user_query": "Tell me all you can about Acme",
        }

        with patch.object(self.service.embed_service, "search_global_chunks", return_value=[summary_chunk, document_chunk]), \
             patch.object(self.service.embed_service, "_rerank_chunks", side_effect=lambda chunks, query, limit: chunks), \
             patch.object(self.service, "_augment_with_deal_summary_candidates", side_effect=lambda chunks, deals: chunks), \
             patch.object(self.service, "_compute_chunk_budgets", return_value=(8, 24)):
            selected, _ = self.service._search_ranked_chunks(plan, [deal])

        self.assertEqual(selected[0]["chunk"], document_chunk)

    def test_search_ranked_chunks_prefers_metric_evidence_when_planner_requests_metrics(self):
        deal = MagicMock()
        deal.id = "deal-1"
        deal.title = "Acme"

        metric_chunk = MagicMock()
        metric_chunk.id = "metric"
        metric_chunk.deal = deal
        metric_chunk.deal_id = deal.id
        metric_chunk.source_type = "document"
        metric_chunk.source_id = "doc-1"
        metric_chunk.metadata = {"title": "metrics.xlsx", "chunk_kind": "metric", "chunk_index": 0}
        metric_chunk.content = "ARR is INR 20 Cr."
        metric_chunk.distance = 0.4
        metric_chunk.rerank_score = 0.6

        risk_chunk = MagicMock()
        risk_chunk.id = "risk"
        risk_chunk.deal = deal
        risk_chunk.deal_id = deal.id
        risk_chunk.source_type = "document"
        risk_chunk.source_id = "doc-2"
        risk_chunk.metadata = {"title": "notes.docx", "chunk_kind": "risk", "chunk_index": 0}
        risk_chunk.content = "Customer concentration remains high."
        risk_chunk.distance = 0.4
        risk_chunk.rerank_score = 0.6

        plan = {
            "query_type": "pipeline_search",
            "named_entities": [],
            "exact_terms": [],
            "semantic_queries": ["ARR and revenue profile"],
            "soft_constraints": [],
            "metric_terms": ["ARR", "revenue"],
            "evidence_preference": "metrics",
            "result_shape": "shortlist",
            "selection_mode": "balanced",
            "stats_mode": "none",
            "global_chunk_limit": 10,
            "user_query": "Show ARR and revenue",
        }

        with patch.object(self.service.embed_service, "search_global_chunks", return_value=[risk_chunk, metric_chunk]), \
             patch.object(self.service.embed_service, "_rerank_chunks", side_effect=lambda chunks, query, limit: chunks), \
             patch.object(self.service, "_augment_with_deal_summary_candidates", side_effect=lambda chunks, deals: chunks), \
             patch.object(self.service, "_compute_chunk_budgets", return_value=(10, 10)):
            selected, _ = self.service._search_ranked_chunks(plan, [deal])

        self.assertEqual(selected[0]["chunk"], metric_chunk)

    def test_search_ranked_chunks_prefers_risk_evidence_when_planner_requests_risks(self):
        deal = MagicMock()
        deal.id = "deal-1"
        deal.title = "Acme"

        metric_chunk = MagicMock()
        metric_chunk.id = "metric"
        metric_chunk.deal = deal
        metric_chunk.deal_id = deal.id
        metric_chunk.source_type = "document"
        metric_chunk.source_id = "doc-1"
        metric_chunk.metadata = {"title": "metrics.xlsx", "chunk_kind": "metric", "chunk_index": 0}
        metric_chunk.content = "ARR is INR 20 Cr."
        metric_chunk.distance = 0.4
        metric_chunk.rerank_score = 0.6

        risk_chunk = MagicMock()
        risk_chunk.id = "risk"
        risk_chunk.deal = deal
        risk_chunk.deal_id = deal.id
        risk_chunk.source_type = "document"
        risk_chunk.source_id = "doc-2"
        risk_chunk.metadata = {"title": "notes.docx", "chunk_kind": "risk", "chunk_index": 0}
        risk_chunk.content = "Customer concentration remains high."
        risk_chunk.distance = 0.4
        risk_chunk.rerank_score = 0.6

        plan = {
            "query_type": "pipeline_search",
            "named_entities": [],
            "exact_terms": [],
            "semantic_queries": ["key risks and concerns"],
            "soft_constraints": [],
            "metric_terms": [],
            "evidence_preference": "risks",
            "result_shape": "shortlist",
            "selection_mode": "balanced",
            "stats_mode": "none",
            "global_chunk_limit": 10,
            "user_query": "What are the risks?",
        }

        with patch.object(self.service.embed_service, "search_global_chunks", return_value=[metric_chunk, risk_chunk]), \
             patch.object(self.service.embed_service, "_rerank_chunks", side_effect=lambda chunks, query, limit: chunks), \
             patch.object(self.service, "_augment_with_deal_summary_candidates", side_effect=lambda chunks, deals: chunks), \
             patch.object(self.service, "_compute_chunk_budgets", return_value=(10, 10)):
            selected, _ = self.service._search_ranked_chunks(plan, [deal])

        self.assertEqual(selected[0]["chunk"], risk_chunk)

    def test_search_ranked_chunks_dedupes_json_artifact_twins(self):
        deal = MagicMock()
        deal.id = "deal-1"
        deal.title = "Acme"

        chunk_pdf = MagicMock()
        chunk_pdf.id = "chunk-pdf"
        chunk_pdf.deal = deal
        chunk_pdf.deal_id = deal.id
        chunk_pdf.source_type = "document"
        chunk_pdf.source_id = "doc-a"
        chunk_pdf.metadata = {"title": "deck.pdf", "chunk_kind": "claim", "chunk_index": 0}
        chunk_pdf.content = "Strong repeat purchase behavior."
        chunk_pdf.distance = 0.42
        chunk_pdf.rerank_score = 0.8

        chunk_json = MagicMock()
        chunk_json.id = "chunk-json"
        chunk_json.deal = deal
        chunk_json.deal_id = deal.id
        chunk_json.source_type = "document"
        chunk_json.source_id = "doc-b"
        chunk_json.metadata = {"title": "deck.pdf.json", "chunk_kind": "claim", "chunk_index": 0}
        chunk_json.content = "Strong repeat purchase behavior."
        chunk_json.distance = 0.42
        chunk_json.rerank_score = 0.8

        plan = {
            "query_type": "pipeline_search",
            "named_entities": [],
            "exact_terms": [],
            "semantic_queries": ["Which deals show repeat purchase behavior?"],
            "soft_constraints": [],
            "metric_terms": [],
            "evidence_preference": "mixed",
            "result_shape": "shortlist",
            "selection_mode": "balanced",
            "stats_mode": "none",
            "global_chunk_limit": 10,
            "user_query": "Which deals show repeat purchase behavior?",
        }

        with patch.object(self.service.embed_service, "search_global_chunks", return_value=[chunk_pdf, chunk_json]), \
             patch.object(self.service.embed_service, "_rerank_chunks", side_effect=lambda chunks, query, limit: chunks), \
             patch.object(self.service, "_augment_with_deal_summary_candidates", side_effect=lambda chunks, deals: chunks), \
             patch.object(self.service, "_compute_chunk_budgets", return_value=(10, 10)):
            selected, diagnostics = self.service._search_ranked_chunks(plan, [deal])

        self.assertEqual(len(selected), 1)
        self.assertEqual(diagnostics["dropped_as_duplicates"], 1)

    def test_search_ranked_chunks_scopes_to_named_comparison_deals(self):
        arman = MagicMock()
        arman.id = "deal-arman"
        arman.title = "Arman Financial Services Limited"

        opl = MagicMock()
        opl.id = "deal-opl"
        opl.title = "Online PSB Loans (OPL)"

        unrelated = MagicMock()
        unrelated.id = "deal-other"
        unrelated.title = "Other Deal"

        plan = {
            "query_type": "comparison",
            "named_entities": [
                {"type": "deal", "text": "Arman Financial", "confidence": 0.9},
                {"type": "deal", "text": "Online PSB", "confidence": 0.9},
            ],
            "exact_terms": [],
            "semantic_queries": ["arman financial", "online psb"],
            "soft_constraints": [],
            "metric_terms": [],
            "evidence_preference": "mixed",
            "result_shape": "named_set",
            "selection_mode": "depth_first",
            "stats_mode": "none",
            "global_chunk_limit": 8,
            "user_query": "what is a better pick between armaan financial and online psb",
        }

        with patch.object(self.service.embed_service, "search_global_chunks", return_value=[] ) as search_global_chunks, \
             patch.object(self.service.embed_service, "_rerank_chunks", side_effect=lambda chunks, query, limit: chunks), \
             patch.object(self.service, "_augment_with_deal_summary_candidates", side_effect=lambda chunks, deals: chunks), \
             patch("ai_orchestrator.services.universal_chat.DocumentChunk.objects") as chunk_manager:
            queryset = MagicMock()
            queryset.select_related.return_value = queryset
            queryset.filter.return_value = queryset
            queryset.order_by.return_value = []
            chunk_manager.all.return_value = queryset

            self.service._search_ranked_chunks(plan, [arman, opl, unrelated])

        _, kwargs = search_global_chunks.call_args
        self.assertEqual(kwargs["deal_ids"], ["deal-arman", "deal-opl"])

    def test_chunk_rerank_document_includes_document_metadata(self):
        deal = MagicMock()
        deal.title = "Acme"

        chunk = MagicMock()
        chunk.deal = deal
        chunk.source_type = "extracted_source"
        chunk.source_id = "doc-1"
        chunk.metadata = {"filename": "annual_report.pdf", "chunk_kind": "normalized_text"}
        chunk.content = "Chunk body text."

        with patch.object(
            self.service,
            "_document_metadata_for_chunk",
            return_value={
                "document_name": "annual_report.pdf",
                "document_type": "Annual Report",
                "citation_label": "annual_report.pdf",
                "document_summary": "Detailed annual report summary.",
                "metrics": [{"revenue": "100"}],
                "tables_summary": ["Income statement"],
                "risks": ["Credit risk"],
            },
        ):
            document = self.service._build_chunk_rerank_document(
                chunk,
                {"evidence_preference": "documents"},
            )

        self.assertIn("Deal: Acme", document)
        self.assertIn("Source Title: annual_report.pdf", document)
        self.assertIn("Document Summary: Detailed annual report summary.", document)
        self.assertIn("Metrics:", document)
        self.assertIn("Tables:", document)
        self.assertIn("Risks:", document)

    def test_stats_mode_count_skips_chunk_retrieval_when_global_limit_zero(self):
        plan = {
            "query_type": "stats",
            "named_entities": [],
            "semantic_queries": ["how many deals do we have"],
            "metric_terms": [],
            "evidence_preference": "summary",
            "result_shape": "cross_pipeline",
            "selection_mode": "breadth_first",
            "stats_mode": "count",
            "global_chunk_limit": 0,
            "user_query": "how many deals do we have",
        }

        selected, diagnostics = self.service._search_ranked_chunks(plan, [])

        self.assertEqual(selected, [])
        self.assertEqual(diagnostics["candidate_chunk_count"], 0)

    def test_build_rerank_query_uses_planner_fields_not_raw_query(self):
        query = self.service._build_rerank_query(
            {
                "named_entities": [{"type": "deal", "text": "Acme Finance", "confidence": 0.9}],
                "semantic_queries": ["fintech lenders with strong collections"],
                "metric_terms": ["ARR", "revenue"],
                "evidence_preference": "metrics",
                "result_shape": "shortlist",
                "selection_mode": "balanced",
                "stats_mode": "none",
                "user_query": "raw user sentence that should not be used directly",
            }
        )

        self.assertIn("named_entities: Acme Finance", query)
        self.assertIn("fintech lenders with strong collections", query)
        self.assertIn("metrics: ARR, revenue", query)
        self.assertIn("evidence: metrics", query)
        self.assertIn("result_shape: shortlist", query)


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
