import json
from uuid import uuid4

from django.test import SimpleTestCase

from ai_orchestrator.services.chat_scope import (
    ChatScopeValidationError,
    internal_citation,
    normalize_web_citation,
    parse_chat_scope,
)
from ai_orchestrator.services.llm_providers import AnthropicProviderService
from ai_orchestrator.services.parsers import ResponseParserService


class ChatScopeContractTests(SimpleTestCase):
    def test_explicit_web_scope_requires_external_provider(self):
        with self.assertRaisesRegex(ChatScopeValidationError, "requires the Anthropic"):
            parse_chat_scope({"model_provider": "vllm", "web_search_enabled": True})

    def test_private_scope_is_deduplicated_and_kept_internal(self):
        document_id = str(uuid4())
        transcript_id = str(uuid4())
        scope = parse_chat_scope(
            {
                "model_provider": "vllm",
                "document_ids": [document_id, document_id],
                "transcript_ids": [transcript_id],
            }
        )
        self.assertEqual(scope.document_ids, [document_id])
        self.assertEqual(scope.transcript_ids, [transcript_id])
        self.assertEqual(scope.evidence_mode, "internal")

    def test_malformed_identifier_is_rejected(self):
        with self.assertRaisesRegex(ChatScopeValidationError, "invalid UUID"):
            parse_chat_scope({"document_ids": ["not-a-uuid"]})

    def test_external_provider_rejects_private_scope(self):
        with self.assertRaisesRegex(ChatScopeValidationError, "cannot be sent"):
            parse_chat_scope(
                {
                    "model_provider": "anthropic",
                    "document_ids": [str(uuid4())],
                }
            )

    def test_internal_and_web_citations_are_structured(self):
        transcript = internal_citation(
            {
                "source_type": "meeting_note",
                "source_id": "note-1",
                "chunk_id": "chunk-1",
                "source_title": "Founder call",
                "text": "Revenue grew.",
                "metadata": {"meeting_at": "2026-07-01T10:00:00Z"},
            }
        )
        web = normalize_web_citation(
            {"url": "https://example.com/report", "title": "Public report"},
            retrieved_at="2026-07-27T00:00:00Z",
        )
        self.assertEqual(transcript["kind"], "transcript")
        self.assertEqual(transcript["meeting_at"], "2026-07-01T10:00:00Z")
        self.assertEqual(web["kind"], "web")
        self.assertIsNone(
            normalize_web_citation(
                {"url": "javascript:alert(1)"},
                retrieved_at="2026-07-27T00:00:00Z",
            )
        )


class AnthropicWebSearchContractTests(SimpleTestCase):
    def _provider(self):
        provider = AnthropicProviderService.__new__(AnthropicProviderService)
        provider.model = "claude-haiku-4-5"
        provider.search_model = "claude-sonnet-4-6"
        return provider

    def test_explicit_off_overrides_keyword_heuristic(self):
        payload = self._provider()._build_anthropic_payload(
            {
                "model": "claude-haiku-4-5",
                "prompt": "Search the latest market news",
                "options": {"web_search_enabled": False, "disable_search": True},
            },
            stream=True,
        )
        self.assertNotIn("tools", payload)

    def test_explicit_on_does_not_bypass_searxng_with_native_search(self):
        payload = self._provider()._build_anthropic_payload(
            {
                "model": "claude-haiku-4-5",
                "prompt": "Tell me about this market",
                "options": {
                    "web_search_enabled": True,
                    "enable_dynamic_web_search": True,
                },
            },
            stream=True,
        )
        self.assertEqual(payload["model"], "claude-haiku-4-5")
        self.assertNotIn("tools", payload)

    def test_stream_parser_preserves_provider_citations(self):
        parsed = list(
            ResponseParserService.parse_stream(
                iter(
                    [
                        json.dumps(
                            {
                                "response": "",
                                "thinking": "",
                                "citations": [
                                    {
                                        "url": "https://example.com",
                                        "title": "Example",
                                    }
                                ],
                            }
                        )
                    ]
                )
            )
        )
        self.assertEqual(parsed[0][0]["citations"][0]["url"], "https://example.com")
