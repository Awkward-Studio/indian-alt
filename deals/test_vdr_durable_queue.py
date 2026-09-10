from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from ai_orchestrator.models import AIAuditLog
from deals.models import Deal
from deals.services import vdr_queue


@override_settings(VDR_DURABLE_QUEUE_ENABLED=True)
class DurableVdrQueueTests(TestCase):
    def make_job(self, title="First", files=("a", "b")):
        deal = Deal.objects.create(title=title, processing_status="processing")
        manifest = [{
            "source_file_id": file_id, "name": f"{file_id}.pdf", "status": "queued",
            "celery_task_id": None, "file_info": {"id": file_id, "name": f"{file_id}.pdf"},
        } for file_id in files]
        audit = AIAuditLog.objects.create(
            source_type="vdr_indexing", source_id=str(deal.id), context_label=title,
            model_used="embed", system_prompt="queued", user_prompt="queued",
            status="PENDING", is_success=False,
            source_metadata=vdr_queue.initial_metadata(
                kind="indexing", manifest=manifest, user_email="test@example.com",
            ),
        )
        return deal, audit

    @patch("deals.services.vdr_queue._high_priority_busy", return_value=False)
    @patch("deals.tasks.process_single_document_async.apply_async")
    def test_dispatches_only_first_document_and_records_generation(self, apply_async, _busy):
        _, audit = self.make_job()
        with self.captureOnCommitCallbacks(execute=True):
            result = vdr_queue.dispatch()
        audit.refresh_from_db()
        metadata = audit.source_metadata
        self.assertEqual(result["unit"], "a")
        self.assertEqual(metadata["dispatch_generation"], 1)
        self.assertEqual(metadata["current_unit_key"], "a")
        self.assertEqual(metadata["document_queue"][0]["status"], "processing")
        self.assertEqual(metadata["document_queue"][1]["status"], "queued")
        apply_async.assert_called_once()

    @patch("deals.services.vdr_queue.kick")
    def test_completion_clears_ownership_and_stale_completion_is_ignored(self, kick):
        _, audit = self.make_job(files=("a",))
        metadata = dict(audit.source_metadata)
        metadata.update({
            "queue_state": "active", "dispatch_generation": 4,
            "current_task_id": "current", "current_unit_type": "document", "current_unit_key": "a",
        })
        audit.status = "PROCESSING"
        audit.source_metadata = metadata
        audit.save()
        self.assertFalse(vdr_queue.unit_finished(
            str(audit.id), task_id="old", generation=3, unit_key="a",
            result={"status": "completed"},
        ))
        self.assertTrue(vdr_queue.unit_finished(
            str(audit.id), task_id="current", generation=4, unit_key="a",
            result={"status": "success", "document_id": "doc-1"},
        ))
        audit.refresh_from_db()
        self.assertEqual(audit.source_metadata["queue_state"], "waiting_next_unit")
        self.assertEqual(audit.source_metadata["document_queue"][0]["status"], "completed")
        self.assertIsNone(audit.source_metadata["current_task_id"])
        kick.assert_called_once()

    @patch("deals.services.vdr_queue.kick")
    @patch("config.celery.app.control.inspect")
    @patch("ai_orchestrator.services.celery_queue_snapshot.CeleryQueueSnapshotService.snapshot")
    def test_reconcile_supersedes_missing_stale_delivery(self, snapshot, inspect, kick):
        _, audit = self.make_job(files=("a",))
        metadata = dict(audit.source_metadata)
        metadata.update({
            "queue_state": "active", "dispatch_generation": 2, "current_task_id": "missing",
            "current_unit_type": "document", "current_unit_key": "a",
            "heartbeat_at": (timezone.now() - timedelta(minutes=6)).isoformat(),
        })
        metadata["document_queue"][0]["status"] = "processing"
        audit.status = "PROCESSING"
        audit.source_metadata = metadata
        audit.save()
        snapshot.return_value = {"messages": [], "unacked": {"messages": []}}
        inspect.return_value.active.return_value = {}
        inspect.return_value.reserved.return_value = {}
        result = vdr_queue.reconcile()
        audit.refresh_from_db()
        self.assertEqual(result["recovered"], 1)
        self.assertEqual(audit.source_metadata["recovery_count"], 1)
        self.assertEqual(audit.source_metadata["dispatch_generation"], 3)
        self.assertEqual(audit.source_metadata["document_queue"][0]["status"], "recovering")
        self.assertIsNone(audit.source_metadata["current_task_id"])

    def test_queue_position_is_fifo_across_indexing_and_report_jobs(self):
        _, first = self.make_job("Indexing", files=("a",))
        report_deal = Deal.objects.create(title="Report")
        second = AIAuditLog.objects.create(
            source_type="deal_full_synthesis", source_id=str(report_deal.id), context_label="Report",
            model_used="model", system_prompt="queued", user_prompt="queued",
            status="PENDING", is_success=False,
            source_metadata=vdr_queue.initial_metadata(
                kind="report", manifest=[{"title": "Executive Summary", "status": "queued"}],
            ),
        )
        self.assertEqual(vdr_queue.queue_position(str(first.id)), 1)
        self.assertEqual(vdr_queue.queue_position(str(second.id)), 2)

    @patch("deals.services.vdr_queue._high_priority_busy", return_value=False)
    @patch("deals.tasks.process_vdr_report_section.apply_async")
    def test_report_dispatches_one_section_on_the_same_lane(self, apply_async, _busy):
        deal = Deal.objects.create(title="Report")
        audit = AIAuditLog.objects.create(
            source_type="deal_full_synthesis", source_id=str(deal.id), context_label="Report",
            model_used="model", system_prompt="queued", user_prompt="queued",
            status="PENDING", is_success=False,
            source_metadata=vdr_queue.initial_metadata(kind="report", manifest=[
                {"position": 1, "title": "First section", "status": "queued"},
                {"position": 2, "title": "Second section", "status": "queued"},
            ]),
        )
        with self.captureOnCommitCallbacks(execute=True):
            result = vdr_queue.dispatch()
        audit.refresh_from_db()
        self.assertEqual(result["unit"], "First section")
        self.assertEqual(audit.source_metadata["report_section_queue"][0]["status"], "processing")
        self.assertEqual(audit.source_metadata["report_section_queue"][1]["status"], "queued")
        apply_async.assert_called_once()
