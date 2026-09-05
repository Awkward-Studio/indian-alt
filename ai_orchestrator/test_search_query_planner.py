import json
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from ai_orchestrator.services.search_provider import SearXNGProviderService
from ai_orchestrator.services.search_query_planner import SearchQueryPlanner


@override_settings(SEARCH_QUERY_PLANNER_ENABLED=True, VLLM_TEXT_MODEL="test-model")
class SearchQueryPlannerTests(SimpleTestCase):
    @patch("ai_orchestrator.services.search_query_planner.SearchQueryPlanner.plan")
    def test_shared_search_executes_generated_queries_with_freshness(self, planner):
        planner.return_value = {"source": "vm", "intent": "news", "queries": ["refined news query"], "time_range": "month"}
        service = SearXNGProviderService()
        service._search_results = Mock(return_value=[{"url": "https://example.com/article", "title": "Report"}])
        results = service.search_many(["original question"])
        self.assertEqual(len(results), 1)
        self.assertEqual(service._search_results.call_args.args[0], "refined news query")
        self.assertEqual(service._search_results.call_args.kwargs["time_range"], "month")

    @patch("ai_orchestrator.services.search_query_planner.SearchQueryPlanner.plan")
    def test_single_query_and_legacy_search_cannot_bypass_inference(self, planner):
        planner.return_value = {"source": "vm", "queries": ["resolved company news"], "time_range": None}
        service = SearXNGProviderService()
        service._search_results = Mock(return_value=[])
        service.search_results("what about them?", context={"company": "Acme"})
        self.assertEqual(planner.call_args.kwargs["context"], {"company": "Acme"})
        service.search("company")
        service.search_many(["company"], plan_queries=False)
        self.assertEqual(planner.call_count, 3)
        self.assertTrue(all(call.args[0] == "resolved company news" for call in service._search_results.call_args_list))

    @patch("ai_orchestrator.services.search_query_planner.SearchQueryPlanner.plan")
    def test_failed_or_disabled_planning_never_contacts_search(self, planner):
        service = SearXNGProviderService()
        service._search_results = Mock()
        for source in ("fallback", "disabled"):
            planner.return_value = {"source": source, "queries": ["unsafe seed"]}
            self.assertEqual(service.search_results("unsafe seed"), [])
            self.assertEqual(service.last_status, "planning_failed")
        service._search_results.assert_not_called()

    def test_context_is_bounded_and_allowlisted(self):
        provider = Mock()
        provider.execute_standard.return_value = {"response": json.dumps({
            "intent": "company", "queries": ["Acme India"], "time_range": "",
        })}
        SearchQueryPlanner(provider).plan(["their competitors"], SearXNGProviderService.sanitize_query,
            context={"company": "Acme", "conversation": "x" * 9000, "private_notes": "secret"})
        payload = json.loads(provider.execute_standard.call_args.args[0]["prompt"])
        self.assertEqual(payload["context"]["company"], "Acme")
        self.assertEqual(len(payload["context"]["conversation"]), 4000)
        self.assertNotIn("private_notes", payload["context"])

    def test_valid_plan_preserves_queries_and_freshness(self):
        provider = Mock()
        provider.execute_standard.return_value = {"response": json.dumps({
            "intent": "news", "queries": ["India payments RBI news", "India payments RBI news"], "time_range": "month",
        })}
        plan = SearchQueryPlanner(provider).plan(["recent payments developments"], SearXNGProviderService.sanitize_query)
        self.assertEqual(plan["source"], "vm")
        self.assertEqual(plan["queries"], ["India payments RBI news"])
        self.assertEqual(plan["time_range"], "month")

    def test_invalid_output_and_timeout_fall_back(self):
        for response in ("not json", '{"intent":"news","queries":[123],"time_range":"day"}'):
            provider = Mock()
            provider.execute_standard.return_value = {"response": response}
            plan = SearchQueryPlanner(provider).plan(["company overview"], SearXNGProviderService.sanitize_query)
            self.assertEqual(plan["queries"], ["company overview"])
            self.assertEqual(plan["source"], "fallback")
        provider.execute_standard.side_effect = TimeoutError()
        self.assertEqual(SearchQueryPlanner(provider).plan(["company"], str)["source"], "fallback")

    @override_settings(SEARCH_QUERY_PLANNER_ENABLED=False)
    def test_disabled_does_not_call_inference(self):
        provider = Mock()
        plan = SearchQueryPlanner(provider).plan(["company"], str)
        provider.execute_standard.assert_not_called()
        self.assertEqual(plan["source"], "disabled")

    def test_generated_queries_are_sanitized(self):
        provider = Mock()
        provider.execute_standard.return_value = {"response": json.dumps({
            "intent": "company", "queries": ["Company owner person@example.com"], "time_range": "",
        })}
        plan = SearchQueryPlanner(provider).plan(["company"], SearXNGProviderService.sanitize_query)
        self.assertNotIn("@", plan["queries"][0])
