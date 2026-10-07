from unittest.mock import patch
from django.test import TestCase
from ai_orchestrator.models import AIPersonality
from ai_orchestrator.services.ai_processor import AIProcessorService


class JsonThinkingTests(TestCase):
    def setUp(self):
        AIPersonality.objects.create(name='JSON test', is_default=True, system_instructions='Return the requested output.')

    @patch('ai_orchestrator.services.ai_processor.log_worker_event')
    def test_json_request_overrides_explicit_thinking_and_preserves_format(self, _events):
        service = AIProcessorService()
        with patch.object(service, '_standard_response', return_value={'response': '{}'}) as execute:
            service.process_content(content='Return JSON', model_override='Qwen/test',
                metadata={'response_mode': 'json', 'response_format': {'type': 'json_object'},
                          'chat_template_kwargs': {'enable_thinking': True}})
        payload = execute.call_args.args[0]
        self.assertFalse(payload['chat_template_kwargs']['enable_thinking'])
        self.assertEqual(payload['response_format'], {'type': 'json_object'})

    @patch('ai_orchestrator.services.ai_processor.log_worker_event')
    def test_default_json_mode_also_disables_thinking(self, _events):
        service = AIProcessorService()
        with patch.object(service, '_standard_response', return_value={'response': '{}'}) as execute:
            service.process_content(content='Return structured data', model_override='Qwen/test',
                metadata={'chat_template_kwargs': {'enable_thinking': True}})
        self.assertFalse(execute.call_args.args[0]['chat_template_kwargs']['enable_thinking'])
