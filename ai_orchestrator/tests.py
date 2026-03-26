from django.test import TestCase
from unittest.mock import MagicMock, patch

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
            "deal_limit": 8,
            "chunks_per_deal": 2,
            "user_query": "compare this with other fintech deals",
        }
        with patch.object(self.service, "_build_query_plan", return_value=expected_plan) as build_query_plan, \
             patch.object(self.service, "_get_candidate_deals", return_value=[]), \
             patch.object(self.service, "_search_ranked_chunks", return_value=[]):
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

    def test_first_turn_without_assistant_history_uses_query_builder(self):
        expected_plan = {
            "query_type": "pipeline_search",
            "deal_filters": {},
            "exact_terms": [],
            "keywords": ["acme"],
            "metric_terms": [],
            "rag_queries": ["Tell me about Acme"],
            "needs_stats": False,
            "deal_limit": 8,
            "chunks_per_deal": 2,
            "user_query": "Tell me about Acme",
        }
        with patch.object(self.service, "_build_query_plan", return_value=expected_plan) as build_query_plan, \
             patch.object(self.service, "_get_candidate_deals", return_value=[]), \
             patch.object(self.service, "_search_ranked_chunks", return_value=[]):
            metadata = self.service.process_intent_and_build_metadata(
                user_message="Tell me about Acme",
                conversation_id="conv-1",
                history_context="",
                audit_log_id="audit-1",
            )

        build_query_plan.assert_called_once()
        self.assertTrue(metadata["used_query_builder"])
        self.assertIn("No prior assistant context", metadata["gate_reason"])
