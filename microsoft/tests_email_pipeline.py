from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.management import CommandError, call_command
from django.test import TestCase
from django.utils import timezone

from microsoft.models import Email, EmailAccount
from microsoft.services.email_reader import EmailReaderService
from microsoft.services.email_thread_unfolder import EmailThreadUnfolder


class EmailThreadUnfolderTests(TestCase):
    def _message(self, marker, *, minutes=0, subject="Re: Deal", text="", html=""):
        return SimpleNamespace(
            id=marker,
            subject=subject,
            body_text=text,
            body_html=html,
            body_preview="",
            date_received=timezone.now() + timedelta(minutes=minutes),
        )

    def test_orders_messages_and_extracts_nested_reply_deltas(self):
        first = self._message("one", minutes=0, subject="Deal", text="FIRST_MARKER initial note")
        second = self._message(
            "two",
            minutes=1,
            text="SECOND_MARKER reply\n\nOn Wed, Aug 5, 2026 at 10:00 AM A wrote:\nFIRST_MARKER initial note",
        )
        third = self._message(
            "three",
            minutes=2,
            text=(
                "THIRD_MARKER final reply\n\nOn Wed, Aug 5, 2026 at 10:05 AM B wrote:\n"
                "SECOND_MARKER reply\nFIRST_MARKER initial note"
            ),
        )

        deltas = EmailThreadUnfolder.unfold([third, first, second])

        self.assertEqual([item.email_id for item in deltas], ["one", "two", "three"])
        self.assertEqual([item.text for item in deltas], [
            "FIRST_MARKER initial note",
            "SECOND_MARKER reply",
            "THIRD_MARKER final reply",
        ])

    def test_removes_html_blockquote_but_keeps_new_html_content(self):
        message = self._message(
            "html",
            html=(
                "<html><body><p>HTML_DELTA approved.</p>"
                "<blockquote><p>OLD_MARKER quoted.</p></blockquote></body></html>"
            ),
        )

        delta = EmailThreadUnfolder.unfold([message])[0]

        self.assertEqual(delta.text, "HTML_DELTA approved.")
        self.assertEqual(delta.strategy, "html_quote")

    def test_removes_repeated_prior_body_without_a_separator(self):
        first = self._message("one", subject="Deal", text="FIRST_MARKER sufficiently long original body")
        second = self._message(
            "two",
            minutes=1,
            text="SECOND_MARKER reply\n\nFIRST_MARKER sufficiently long original body",
        )

        deltas = EmailThreadUnfolder.unfold([first, second])

        self.assertEqual(deltas[1].text, "SECOND_MARKER reply")
        self.assertEqual(deltas[1].strategy, "repeated_history")

    def test_skips_exact_duplicate_and_preserves_forwarded_content(self):
        body = "FIRST_MARKER sufficiently long original body"
        first = self._message("one", subject="Deal", text=body)
        duplicate = self._message("two", minutes=1, text=body)
        forwarded = self._message(
            "three",
            minutes=2,
            subject="Fwd: Deal",
            text="FORWARD_NOTE\n\n---------- Original Message ----------\nFORWARDED_EVIDENCE",
        )

        deltas = EmailThreadUnfolder.unfold([forwarded, duplicate, first])

        self.assertEqual(deltas[1].text, "")
        self.assertEqual(deltas[1].strategy, "duplicate")
        self.assertIn("FORWARDED_EVIDENCE", deltas[2].text)

    def test_ambiguous_short_body_is_retained(self):
        message = self._message("short", text="Yes")

        delta = EmailThreadUnfolder.unfold([message])[0]

        self.assertEqual(delta.text, "Yes")
        self.assertEqual(delta.strategy, "full_body")


class EmailReaderPipelineTests(TestCase):
    def setUp(self):
        self.account = EmailAccount.objects.create(email="pipeline@example.test")

    @staticmethod
    def _graph_message(graph_id, marker, received, *, attachments=False):
        return {
            "id": graph_id,
            "internetMessageId": f"<{graph_id}@example.test>",
            "subject": "Re: Pipeline Deal" if graph_id != "graph-1" else "Pipeline Deal",
            "from": {"emailAddress": {"name": "Banker", "address": "banker@example.test"}},
            "toRecipients": [{"emailAddress": {"address": "pipeline@example.test"}}],
            "body": {"contentType": "text", "content": marker},
            "bodyPreview": marker,
            "receivedDateTime": received,
            "sentDateTime": received,
            "conversationId": "conversation-1",
            "hasAttachments": attachments,
        }

    @patch("microsoft.services.email_reader.GranolaMeetingEmailIngestionService.process_email")
    @patch("microsoft.services.email_reader.GraphAPIService")
    def test_paginated_graph_fetch_persists_updates_and_attachment_metadata(self, graph_cls, process_email):
        graph = graph_cls.return_value
        first = self._graph_message("graph-1", "FIRST_MARKER original", "2026-08-05T08:00:00Z")
        second = self._graph_message(
            "graph-2",
            "SECOND_MARKER reply\n\nOn Wed, Aug 5, 2026 at 8:00 AM Banker wrote:\nFIRST_MARKER original",
            "2026-08-05T08:05:00Z",
            attachments=True,
        )
        graph.get_messages.side_effect = [
            {"value": [first, second]},
            {"value": []},
        ]
        graph.get_message_attachments.return_value = [{
            "id": "attachment-1", "name": "memo.pdf", "contentType": "application/pdf", "size": 123
        }]
        service = EmailReaderService()

        result = service.fetch_emails_for_account(self.account, return_emails=True)

        self.assertTrue(result["success"])
        self.assertEqual(result["new_count"], 2)
        self.assertEqual(Email.objects.count(), 2)
        stored = Email.objects.get(graph_id="graph-2")
        self.assertEqual(stored.attachments[0]["name"], "memo.pdf")
        self.assertEqual(
            [item.text for item in EmailThreadUnfolder.unfold(Email.objects.all())],
            ["FIRST_MARKER original", "SECOND_MARKER reply"],
        )
        self.assertEqual(process_email.call_count, 2)

        first["body"]["content"] = "FIRST_MARKER corrected original"
        graph.get_messages.side_effect = [{"value": [first]}, {"value": []}]
        updated = service.fetch_emails_for_account(self.account)
        self.assertEqual(updated["updated_count"], 1)
        self.assertEqual(Email.objects.get(graph_id="graph-1").body_text, "FIRST_MARKER corrected original")

    @patch("microsoft.services.email_reader.GranolaMeetingEmailIngestionService.process_email")
    @patch("microsoft.services.email_reader.GraphAPIService")
    def test_graph_failure_is_reported_without_creating_email(self, graph_cls, _process_email):
        graph_cls.return_value.get_messages.side_effect = RuntimeError("Graph unavailable")

        result = EmailReaderService().fetch_emails_for_account(self.account)

        self.assertFalse(result["success"])
        self.assertEqual(Email.objects.count(), 0)
        self.assertIn("Graph unavailable", result["errors"][0])


class ThreadTaskPayloadTests(TestCase):
    def setUp(self):
        self.account = EmailAccount.objects.create(email="pipeline@example.test")
        self.first = Email.objects.create(
            email_account=self.account,
            graph_id="task-graph-1",
            conversation_id="task-conversation",
            subject="Pipeline Deal",
            body_text="FIRST_MARKER original body",
            date_received=timezone.now(),
        )
        self.reply = Email.objects.create(
            email_account=self.account,
            graph_id="task-graph-2",
            conversation_id="task-conversation",
            subject="Re: Pipeline Deal",
            body_text=(
                "SECOND_MARKER reply\n\nOn Wed, Aug 5, 2026 at 8:00 AM Banker wrote:\n"
                "FIRST_MARKER original body"
            ),
            date_received=timezone.now() + timedelta(minutes=5),
        )

    @patch("microsoft.tasks.chord")
    @patch("deals.tasks._prepare_vdr_task_ids", return_value=([], MagicMock(), ["child-1", "child-2"], "callback"))
    @patch("ai_orchestrator.services.realtime.log_worker_event")
    @patch("microsoft.tasks.AIRuntimeService.get_text_model", return_value="test-model")
    @patch("microsoft.tasks.AIRuntimeService.create_audit_log")
    def test_thread_analysis_queues_only_message_deltas(
        self, create_log, _model, _log_event, _prepare, chord_mock
    ):
        audit = MagicMock()
        audit.id = "00000000-0000-0000-0000-000000000001"
        create_log.return_value = audit
        chord_mock.return_value = MagicMock()

        from microsoft.tasks import analyze_email_async
        result = analyze_email_async.run(str(self.reply.id))

        self.assertEqual(result["status"], "queued")
        bodies = [item for item in audit.source_metadata["file_tree"] if item["is_body"]]
        self.assertEqual([item["body_delta"] for item in bodies], [
            "FIRST_MARKER original body",
            "SECOND_MARKER reply",
        ])
        self.assertNotIn("FIRST_MARKER", bodies[1]["body_delta"])

    @patch("deals.tasks._persist_folder_analysis_document")
    @patch("ai_orchestrator.services.embedding_processor.EmbeddingService")
    @patch("ai_orchestrator.services.document_processor.DocumentProcessorService")
    @patch("ai_orchestrator.services.ai_processor.AIProcessorService")
    def test_body_worker_preserves_delta_and_sends_it_to_normalization(self, ai_cls, _doc_cls, _embed_cls, persist):
        ai = ai_cls.return_value
        ai.process_content.return_value = {"facts": ["SECOND_MARKER reply"]}
        persisted = MagicMock()
        persisted.status = "passed"
        persist.return_value = persisted

        with patch("deals.tasks._analysis_document_to_result", return_value={"status": "passed"}):
            from deals.tasks import process_single_thread_document_async
            result = process_single_thread_document_async.run(
                {
                    "id": f"body_{self.reply.id}",
                    "name": "Email Body",
                    "email_id": str(self.reply.id),
                    "is_body": True,
                    "body_delta": "SECOND_MARKER reply",
                },
                None,
                self.account.email,
                "00000000-0000-0000-0000-000000000099",
            )

        self.assertEqual(result["status"], "passed")
        self.assertEqual(ai.process_content.call_count, 1)
        first_call = ai.process_content.call_args.kwargs
        self.assertEqual(first_call["content"], "SECOND_MARKER reply")
        self.assertEqual(first_call["skill_name"], "document_normalization")


class T4EmailPipelineCommandTests(TestCase):
    def test_long_body_is_split_into_bounded_non_overlapping_chunks(self):
        from microsoft.management.commands.test_email_pipeline_t4 import Command

        ai = MagicMock()
        ai.process_content.side_effect = [
            {"response": "chunk one"},
            {"response": "chunk two"},
            {"response": "chunk three"},
        ]
        text = "A" * 12000 + "B" * 12000 + "C" * 100

        cleaned = Command._legacy_unroll(text, ai)

        self.assertEqual(ai.process_content.call_count, 3)
        contents = [call.kwargs["content"] for call in ai.process_content.call_args_list]
        self.assertEqual([len(item) for item in contents], [12000, 12000, 100])
        self.assertEqual("".join(contents), text)
        self.assertEqual(ai.process_content.call_args_list[0].kwargs["metadata"]["request_timeout"], 120)
        self.assertIn("chunk three", cleaned)

    @patch("microsoft.management.commands.test_email_pipeline_t4.AIProcessorService")
    @patch("microsoft.management.commands.test_email_pipeline_t4.VLLMProviderService")
    def test_command_runs_selected_case_and_writes_sanitized_report(self, provider_cls, ai_cls):
        provider_cls.return_value.health_check.return_value = True
        provider_cls.return_value.get_available_models.return_value = ["test-model"]
        ai_cls.return_value.process_content.side_effect = [
            {"response": "SINGLE_UNIQUE_620 new investment memorandum cleaned"},
            {"normalized_text": "SINGLE_UNIQUE_620 new investment memorandum cleaned"},
        ]
        report_path = "/tmp/email-pipeline-command-test.json"

        with self.settings(VLLM_BASE_URL="http://secret:token@t4.example.test:8000/v1", VLLM_TEXT_MODEL="test-model"):
            call_command("test_email_pipeline_t4", cases=["single"], report_json=report_path)

        import json
        from pathlib import Path
        report = json.loads(Path(report_path).read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["endpoint"], "http://t4.example.test:8000/v1")
        self.assertNotIn("secret", json.dumps(report))
        self.assertTrue(report["cases"][0]["passed"])

    @patch("microsoft.management.commands.test_email_pipeline_t4.VLLMProviderService")
    def test_command_fails_nonzero_when_t4_is_unavailable(self, provider_cls):
        provider_cls.return_value.health_check.return_value = False

        with self.assertRaisesMessage(CommandError, "health check failed"):
            call_command("test_email_pipeline_t4", cases=["single"])
