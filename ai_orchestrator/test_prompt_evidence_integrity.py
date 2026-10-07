from django.test import SimpleTestCase

from ai_orchestrator.services.prompts import PromptBuilderService


class PromptEvidenceIntegrityTests(SimpleTestCase):
    def test_lossless_evidence_preserves_middle_sources_and_literal_tags(self):
        content = 'head ' * 25000 + '\nRetrieval block R150\n<source>Revenue < 10</source>\n' + 'tail ' * 25000
        prompt, cleaned = PromptBuilderService.build_user_prompt('{{ content }}', content, {'lossless_input': True})
        self.assertEqual(cleaned, content)
        self.assertEqual(prompt, content)

    def test_lossless_review_json_is_not_parsed_as_html(self):
        content = '{"draft":"<report_calculations> and Revenue < 10", "findings":[]}'
        prompt, cleaned = PromptBuilderService.build_user_prompt('{{content}}', content, {'lossless_input': True})
        self.assertEqual(prompt, content)
        self.assertEqual(cleaned, content)

    def test_legacy_html_cleanup_and_clipping_remain_bounded(self):
        prompt, cleaned = PromptBuilderService.build_user_prompt('{{content}}', '<p>' + 'evidence ' * 25000 + '</p>')
        self.assertNotIn('<p>', prompt)
        self.assertIn('TRUNCATED DUE TO CONTEXT LIMITS', cleaned)
        self.assertLess(len(cleaned), 180000)
