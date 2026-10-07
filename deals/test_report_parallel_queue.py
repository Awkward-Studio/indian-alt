from unittest.mock import patch

from django.test import TestCase, override_settings

from ai_orchestrator.models import AIAuditLog
from ai_orchestrator.prompt_contracts import IC_SECTION_TITLES
from deals.models import Deal
from deals.services import vdr_queue


@override_settings(VDR_DURABLE_QUEUE_ENABLED=True, VDR_DURABLE_REPORT_CONCURRENCY=4)
class ParallelReportQueueTests(TestCase):
    def make_job(self, completed=()):
        deal = Deal.objects.create(title="Financial model test")
        return AIAuditLog.objects.create(source_type="deal_full_synthesis", source_id=str(deal.id),
            status="PENDING", model_used="test", system_prompt="", user_prompt="", raw_response="",
            source_metadata=vdr_queue.initial_metadata(kind="report", manifest=[
                {"title": title, "status": "completed" if title in completed else "queued"}
                for title in IC_SECTION_TITLES
            ]))

    @patch("deals.services.vdr_queue._email_priority_busy", return_value=False)
    @patch("deals.services.vdr_queue._high_priority_busy", return_value=False)
    @patch("deals.tasks.process_vdr_report_section.apply_async")
    def test_canonical_inputs_are_generated_before_four_dependent_sections(self, deliver, *_):
        audit = self.make_job()
        with self.captureOnCommitCallbacks(execute=True):
            vdr_queue.dispatch()
        self.assertEqual(deliver.call_count, 2)
        self.assertEqual({call.kwargs["kwargs"]["section_title"] for call in deliver.call_args_list}, {"Key Financials", "Transaction Details"})

    @patch("deals.services.vdr_queue._email_priority_busy", return_value=False)
    @patch("deals.services.vdr_queue._high_priority_busy", return_value=False)
    @patch("deals.services.vdr_queue.kick")
    @patch("deals.tasks.process_vdr_report_section.apply_async")
    def test_four_sections_keep_independent_ownership_and_completion(self, deliver, *_):
        audit = self.make_job(completed=("Key Financials", "Transaction Details"))
        with self.captureOnCommitCallbacks(execute=True):
            vdr_queue.dispatch()
        self.assertEqual(deliver.call_count, 4)
        audit.refresh_from_db()
        owners = audit.source_metadata["active_report_units"]
        generation = audit.source_metadata["dispatch_generation"]
        for title, owner in owners.items():
            self.assertTrue(vdr_queue.delivery_is_current(str(audit.id), task_id=owner["current_task_id"], generation=generation, unit_key=title))
        title, owner = list(owners.items())[-1]
        self.assertTrue(vdr_queue.unit_finished(str(audit.id), task_id=owner["current_task_id"], generation=generation,
            unit_key=title, result={"status": "completed", "section": f"## {title}\n\nVerified analysis."}))
        audit.refresh_from_db()
        self.assertEqual(len(audit.source_metadata["active_report_units"]), 3)
        self.assertEqual(audit.source_metadata["queue_state"], "active")
        self.assertFalse(vdr_queue.unit_finished(str(audit.id), task_id=owner["current_task_id"], generation=generation,
            unit_key=title, result={"status": "failed"}))

    def test_summary_and_actions_wait_for_substantive_sections(self):
        completed = set(IC_SECTION_TITLES) - {"Executive Summary", "Next Steps"}
        audit = self.make_job(completed=completed)
        self.assertEqual([item["title"] for item in vdr_queue._next_report_batch(audit.source_metadata)], ["Executive Summary", "Next Steps"])
