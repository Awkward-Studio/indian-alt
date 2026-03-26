from django.test import TestCase

from ai_orchestrator.services.parsers import ResponseParserService


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
