import asyncio
import json
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import web
from django.test import SimpleTestCase, TestCase, override_settings

from ai_orchestrator.services.slot_request import (
    SlotClock, SlotProcessingTimeout, InferenceDeliveryError, _execute,
)
from ai_orchestrator.services.llm_providers import VLLMProviderService
from ai_orchestrator.services.realtime import _send_audit_event, broadcast_audit_log_update


class SlotClockTests(SimpleTestCase):
    def test_waiting_idle_and_unknown_intervals_do_not_consume_processing_budget(self):
        clock = SlotClock(7)
        clock.observe({"is_processing": False, "id_task": 7}, 0)
        clock.observe({"is_processing": False, "id_task": 7}, 5000)
        clock.observe({"is_processing": True, "id_task": 8}, 5001)
        clock.observe({"is_processing": True, "id_task": 8}, 5003)
        clock.observe(None, 5100)
        clock.observe({"is_processing": True, "id_task": 8}, 5200)
        clock.observe({"is_processing": True, "id_task": 8}, 5203)
        clock.observe({"is_processing": False, "id_task": 8}, 6000)
        self.assertEqual(clock.active_seconds, 5)
        self.assertEqual(clock.task_id, 8)

    def test_other_task_is_not_charged_to_current_request(self):
        clock = SlotClock(1)
        clock.observe({"is_processing": True, "id_task": 1}, 0)
        clock.observe({"is_processing": True, "id_task": 1}, 100)
        self.assertEqual(clock.active_seconds, 0)

    @override_settings(AI_AUDIT_BROADCAST_TIMEOUT=0.01)
    def test_stalled_notification_is_bounded(self):
        layer = MagicMock()
        async def stalled(*args):
            await asyncio.Event().wait()
        layer.group_send = stalled
        with self.assertRaises(TimeoutError):
            _send_audit_event(layer, "audit", {})

    @patch("ai_orchestrator.services.realtime._broadcast_audit_log_update", side_effect=RuntimeError("Redis offline"))
    def test_notification_failure_does_not_escape_completion(self, send):
        broadcast_audit_log_update(MagicMock(), done=True)


class SlotTransportTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        config = override_settings(AI_SLOT_POLL_SECONDS=0.1, AI_SLOT_RESPONSE_GRACE_SECONDS=0.3)
        config.enable()
        self.addCleanup(config.disable)
        self.task_id = 1
        self.active = False
        self.mode = "success"
        self.posts = 0
        self.events = []
        self.loading = 0
        async def slots(request):
            if self.loading:
                self.loading -= 1
                return web.json_response({}, status=503)
            return web.json_response([{"id": 0, "id_task": self.task_id, "is_processing": self.active}])
        async def complete(request):
            body = await request.json()
            self.assertEqual(body["id_slot"], 0)
            self.posts += 1
            self.task_id += 1
            self.active = True
            try:
                await asyncio.sleep(0.22)
                if self.mode == "active_hang":
                    await asyncio.sleep(1)
                self.active = False
                if self.mode == "idle_hang":
                    await asyncio.sleep(1)
                if body.get("stream"):
                    response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
                    await response.prepare(request)
                    # Split the first JSON object across writes. Real SSE
                    # responses do not guarantee one network chunk per line.
                    await response.write(b'data: {"choices":[{"delta":{"content":"d')
                    await response.write(b'one"}}]}\n\n')
                    await response.write(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n')
                    await response.write(b"data: [DONE]\n\n")
                    await response.write_eof()
                    return response
                return web.json_response({"choices": [{"message": {"content": "done"}, "finish_reason": "stop"}]})
            finally:
                self.active = False
        app = web.Application()
        app.router.add_get("/slots", slots)
        app.router.add_post("/v1/chat/completions", complete)
        self.runner = web.AppRunner(app, shutdown_timeout=0.01)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.provider = MagicMock(base_url=f"http://127.0.0.1:{port}/v1", connect_timeout=1)
        self.provider._headers.return_value = {}
        self.provider._get_completions_url.return_value = f"http://127.0.0.1:{port}/v1/chat/completions"

    async def asyncTearDown(self):
        await self.runner.cleanup()

    def progress(self, **event):
        self.events.append(event)

    async def test_loading_wait_does_not_exhaust_active_allowance_and_next_request_runs(self):
        self.loading = 5
        for _ in range(2):
            result = await _execute(self.provider, {}, 0.4, self.progress)
            self.assertEqual(result["choices"][0]["message"]["content"], "done")
        self.assertEqual(self.posts, 2)
        self.assertEqual(sum(e.get("inference_state") == "response_received" for e in self.events), 2)

    async def test_active_hang_is_processing_timeout(self):
        self.mode = "active_hang"
        with self.assertRaises(SlotProcessingTimeout):
            await _execute(self.provider, {}, 0.15, self.progress)

    async def test_idle_without_response_is_delivery_failure(self):
        self.mode = "idle_hang"
        with self.assertRaises(InferenceDeliveryError):
            await _execute(self.provider, {}, 20, self.progress)
        self.assertFalse(any(e.get("inference_failure_kind") == "slot_processing_timeout" for e in self.events))

    async def test_cancellation_while_waiting_never_submits(self):
        self.loading = 100
        def cancelled(**event):
            raise RuntimeError("cancelled")
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            await _execute(self.provider, {}, 1, cancelled)
        self.assertEqual(self.posts, 0)


class ChatStreamTransportTests(SimpleTestCase):
    def test_finish_chunk_ends_stream_without_waiting_for_done_marker(self):
        response = MagicMock()
        response.iter_lines.return_value = [
            'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        ]
        response.__enter__.return_value = response
        provider = VLLMProviderService()
        with patch("ai_orchestrator.services.llm_providers.requests.post", return_value=response):
            chunks = [json.loads(chunk) for chunk in provider.execute_stream({"prompt": "Reply with OK."})]

        self.assertEqual(chunks, [
            {"response": "OK", "thinking": "", "done": False},
            {"response": "", "thinking": "", "done": True},
        ])
        response.__enter__.assert_called_once_with()
        response.__exit__.assert_called_once()


class StandardTransportSelectionTests(SimpleTestCase):
    @patch("ai_orchestrator.services.ai_processor.InferenceQueueLease")
    @patch("ai_orchestrator.services.ai_processor.broadcast_audit_log_update")
    def test_report_section_uses_slot_watchdog(self, _broadcast, lease_class):
        from ai_orchestrator.services.ai_processor import AIProcessorService

        audit = MagicMock(
            source_type="vdr_report_section", source_metadata={}, status="PROCESSING",
            skill=None, user_prompt="", context_label="VDR report section",
        )
        lease = lease_class.return_value.__enter__.return_value
        service = AIProcessorService.__new__(AIProcessorService)
        service.current_provider = MagicMock()
        service.current_provider.execute_standard.return_value = {
            "response": "## Executive Summary\n\nComplete evidence-backed report section.",
        }

        service._standard_response(
            {"model": "test", "_serialize_inference": True, "_request_timeout": 1800},
            audit,
            "markdown",
        )

        kwargs = service.current_provider.execute_standard.call_args.kwargs
        self.assertEqual(kwargs["timeout"], 1800)
        self.assertIs(kwargs["slot_progress"], lease.record_slot_progress)


class TokenUsageTests(SimpleTestCase):
    def setUp(self):
        from ai_orchestrator.services.ai_processor import AIProcessorService
        self.service = AIProcessorService
        self.audit = MagicMock(system_prompt="System rules", user_prompt="User evidence")

    def test_provider_usage_is_split_and_marked_exact(self):
        self.service._record_token_usage(
            self.audit,
            {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
            "answer",
            "",
        )
        self.assertEqual(self.audit.input_tokens, 120)
        self.assertEqual(self.audit.output_tokens, 30)
        self.assertEqual(self.audit.tokens_used, 150)
        self.assertFalse(self.audit.token_count_is_estimate)

    def test_missing_provider_usage_estimates_input_and_output_separately(self):
        self.service._record_token_usage(self.audit, {}, "answer", "reasoning")
        self.assertGreater(self.audit.input_tokens, 0)
        self.assertGreater(self.audit.output_tokens, 0)
        self.assertEqual(self.audit.tokens_used, self.audit.input_tokens + self.audit.output_tokens)
        self.assertTrue(self.audit.token_count_is_estimate)

    def test_parent_usage_sums_child_calls_and_marks_mixed_counts(self):
        from ai_orchestrator.serializers import token_usage

        parent = MagicMock(
            input_tokens=None, output_tokens=None, tokens_used=None,
            token_count_is_estimate=None,
        )
        children = [
            MagicMock(input_tokens=100, output_tokens=20, tokens_used=120, token_count_is_estimate=False),
            MagicMock(input_tokens=80, output_tokens=30, tokens_used=110, token_count_is_estimate=True),
        ]
        result = token_usage(parent, children)
        self.assertEqual(result["aggregate"]["input_tokens"], 180)
        self.assertEqual(result["aggregate"]["output_tokens"], 50)
        self.assertEqual(result["aggregate"]["total_tokens"], 230)
        self.assertEqual(result["aggregate"]["method"], "mixed")


class WorkflowGuardTests(SimpleTestCase):
    @patch("ai_orchestrator.models.AIAuditLog.objects")
    def test_terminal_and_deleted_workflows_stop_redelivery(self, objects):
        from deals.tasks import _is_cancel_requested
        for row in (None, {"status": "FAILED", "source_metadata": {}}, {"status": "COMPLETED", "source_metadata": {}}):
            objects.filter.return_value.values.return_value.first.return_value = row
            self.assertTrue(_is_cancel_requested("parent"))
        objects.filter.return_value.values.return_value.first.return_value = {"status": "PROCESSING", "source_metadata": {}}
        self.assertFalse(_is_cancel_requested("parent"))


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}},
)
class SegmentPersistenceTests(TestCase):
    def setUp(self):
        from django.core.cache import cache
        from ai_orchestrator.models import AIAuditLog
        from ai_orchestrator.services.ai_processor import AIProcessorService
        cache.clear()
        self.audit = AIAuditLog.objects.create(source_type="document_evidence_segment", status="PROCESSING")
        self.service = AIProcessorService.__new__(AIProcessorService)
        self.service.current_provider = MagicMock()
        self.service.current_provider.execute_standard.return_value = {
            "response": '{"document_summary":"Complete evidence"}', "raw": {"choices": [{"finish_reason": "stop"}]},
        }

    def run_segment(self):
        return self.service._standard_response(
            {"model": "test", "_serialize_inference": True, "_request_timeout": 10}, self.audit, "json",
        )

    def test_success_is_persisted_and_releases_lease_before_next_segment(self):
        from django.core.cache import cache
        from ai_orchestrator.models import AIAuditLog
        self.assertNotIn("error", self.run_segment())
        self.audit.refresh_from_db()
        self.assertEqual(self.audit.status, "COMPLETED")
        self.assertIsNotNone(self.audit.completed_at)
        self.assertEqual(self.audit.source_metadata["inference_state"], "completed")
        self.assertIsNone(cache.get("ai:inference:lease:v1"))
        self.audit = AIAuditLog.objects.create(source_type="document_evidence_segment", status="PROCESSING")
        self.assertNotIn("error", self.run_segment())
        self.assertEqual(self.service.current_provider.execute_standard.call_count, 2)

    def test_processing_timeout_is_persisted_and_releases_lease(self):
        from django.core.cache import cache
        self.service.current_provider.execute_standard.side_effect = SlotProcessingTimeout("active limit")
        self.assertIn("error", self.run_segment())
        self.audit.refresh_from_db()
        self.assertEqual(self.audit.status, "FAILED")
        self.assertIsNotNone(self.audit.completed_at)
        self.assertIsNone(cache.get("ai:inference:lease:v1"))

    def test_truncated_response_is_not_completed(self):
        self.service.current_provider.execute_standard.return_value["raw"]["choices"][0]["finish_reason"] = "length"
        self.assertIn("error", self.run_segment())
        self.audit.refresh_from_db()
        self.assertEqual(self.audit.status, "FAILED")

    def test_incomplete_json_cannot_be_repaired_into_completed_checkpoint(self):
        self.service.current_provider.execute_standard.return_value["response"] = '{"document_summary":"Partial evidence"'
        self.assertIn("error", self.run_segment())
        self.audit.refresh_from_db()
        self.assertEqual(self.audit.status, "FAILED")

    def test_literal_backslash_in_complete_json_is_accepted(self):
        self.service.current_provider.execute_standard.return_value["response"] = (
            '{"document_summary":"Path C:\\Plans\\FY26"}'
        )

        result = self.run_segment()

        self.assertNotIn("error", result)
        self.audit.refresh_from_db()
        self.assertEqual(self.audit.status, "COMPLETED")

    def test_raw_control_character_in_complete_json_is_escaped(self):
        self.service.current_provider.execute_standard.return_value["response"] = (
            '{"document_summary":"Line one' + chr(10) + 'Line two"}'
        )

        result = self.run_segment()

        self.assertNotIn("error", result)
        self.audit.refresh_from_db()
        self.assertEqual(self.audit.status, "COMPLETED")

    def test_structured_quality_flag_does_not_crash_segment_validation(self):
        self.service.current_provider.execute_standard.return_value["response"] = json.dumps({
            "document_summary": "Complete evidence",
            "quality_flags": [{"issue": "Ambiguous source label"}],
        })

        result = self.run_segment()

        self.assertNotIn("error", result)
        self.audit.refresh_from_db()
        self.assertEqual(self.audit.status, "COMPLETED")

    def test_admin_cancellation_is_not_overwritten_by_late_success(self):
        from ai_orchestrator.models import AIAuditLog
        def response(*args, **kwargs):
            AIAuditLog.objects.filter(pk=self.audit.pk).update(status="FAILED", error_message="admin cancelled")
            return {"response": '{"document_summary":"Late result"}'}
        self.service.current_provider.execute_standard.side_effect = response
        self.assertIn("error", self.run_segment())
        self.audit.refresh_from_db()
        self.assertEqual(self.audit.error_message, "admin cancelled")
        self.assertEqual(self.audit.status, "FAILED")

    @patch("ai_orchestrator.services.inference_queue.time.sleep")
    def test_terminal_owner_does_not_block_next_segment(self, sleep):
        from django.core.cache import cache
        from ai_orchestrator.models import AIAuditLog
        from ai_orchestrator.services.inference_queue import InferenceQueueLease
        old = AIAuditLog.objects.create(source_type="document_evidence_segment", status="FAILED")
        cache.set(InferenceQueueLease.KEY, {"audit_log_id": str(old.pk), "lease_token": "old"})
        self.assertNotIn("error", self.run_segment())
        self.assertIsNone(cache.get(InferenceQueueLease.KEY))

    def test_completed_audit_recovers_checkpoint_without_another_model_request(self):
        from ai_orchestrator.models import AIAuditLog
        from deals.services.document_artifacts import DocumentArtifactService
        from django.core.cache import cache
        service = MagicMock()
        with patch.object(DocumentArtifactService, "_segment_cache_key", return_value="checkpoint"):
            AIAuditLog.objects.create(
                source_type="document_evidence_segment", status="COMPLETED", is_success=True,
                source_metadata={"artifact_segment_cache_key": "checkpoint"},
                parsed_json={"document_summary": "Recovered evidence", "quality_flags": []},
            )
            cache.clear()
            artifact = DocumentArtifactService.build_document_artifact(
                file_name="memo.pdf", extracted_text="Complete evidence", ai_service=service,
                source_metadata={"artifact_run_id": "run"},
            )
        service.process_content.assert_not_called()
        self.assertEqual(artifact["source_metadata"]["artifact_segments_completed"], 1)
