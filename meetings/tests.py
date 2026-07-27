import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from django.test import SimpleTestCase

from meetings.services.meeting_signal_analysis import MeetingSignalAnalysisService


class MeetingSignalAnalysisAuditTests(SimpleTestCase):
    def setUp(self):
        self.deal = SimpleNamespace(id=uuid4(), title="Audit Deal")
        self.note = SimpleNamespace(
            id=uuid4(),
            title="Management call",
            meeting_at=None,
            summary="Revenue grew.",
            body="Management discussed revenue growth and customer concentration.",
        )

    @patch("meetings.services.meeting_signal_analysis.AIAuditLog.objects.create")
    def test_success_persists_completed_ai_history_entry(self, create_audit_log):
        audit_log = MagicMock(
            id=uuid4(),
            source_metadata={"workflow": "cross_meeting_signal_analysis"},
        )
        create_audit_log.return_value = audit_log
        service = MeetingSignalAnalysisService()
        service.base_urls = ["http://lm-studio.test/v1"]
        service._broadcast_audit = MagicMock()
        raw_result = {
            "executive_summary": "Growth is positive, with concentration risk.",
            "green_signals": [],
            "red_signals": [],
            "open_questions": ["Provide concentration data."],
        }
        service._post_chat_completion = MagicMock(
            return_value={
                "choices": [{"message": {"content": json.dumps(raw_result)}}],
            }
        )

        result = service.analyze_deal(self.deal, [self.note])

        self.assertEqual(result["audit_log_id"], str(audit_log.id))
        self.assertEqual(audit_log.status, "COMPLETED")
        self.assertTrue(audit_log.is_success)
        self.assertEqual(audit_log.parsed_json, result)
        self.assertEqual(
            create_audit_log.call_args.kwargs["source_type"],
            "meeting_signal_analysis",
        )
        audit_log.save.assert_called_once()
        service._broadcast_audit.assert_called_with(audit_log, done=True)

    @patch("meetings.services.meeting_signal_analysis.AIAuditLog.objects.create")
    def test_failure_persists_failed_ai_history_entry(self, create_audit_log):
        audit_log = MagicMock(
            id=uuid4(),
            source_metadata={"workflow": "cross_meeting_signal_analysis"},
        )
        create_audit_log.return_value = audit_log
        service = MeetingSignalAnalysisService()
        service.base_urls = ["http://lm-studio.test/v1"]
        service._broadcast_audit = MagicMock()
        service._post_chat_completion = MagicMock(side_effect=RuntimeError("offline"))

        with self.assertRaisesRegex(RuntimeError, "LM Studio analysis failed"):
            service.analyze_deal(self.deal, [self.note])

        self.assertEqual(audit_log.status, "FAILED")
        self.assertFalse(audit_log.is_success)
        self.assertIn("offline", audit_log.error_message)
        audit_log.save.assert_called_once()
        service._broadcast_audit.assert_called_with(audit_log, done=True)
