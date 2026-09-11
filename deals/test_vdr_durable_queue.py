from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from ai_orchestrator.models import AIAuditLog
from deals.models import Deal
from deals.services import vdr_queue


class HighPriorityBusyTests(SimpleTestCase):
    @patch("config.celery.app.control.inspect")
    @patch("ai_orchestrator.services.celery_queue_snapshot.CeleryQueueSnapshotService.snapshot")
    def test_current_vdr_delivery_does_not_block_itself(self, snapshot, inspect):
        snapshot.return_value = {
            "queues": [{"name": "high_priority", "ready_count": 0}],
            "unacked": {"messages": [{
                "queue": "high_priority",
                "task_id": "current-vdr-task",
            }]},
        }
        inspect.return_value.active.return_value = {
            "worker": [{
                "id": "current-vdr-task",
                "delivery_info": {"routing_key": "high_priority"},
            }],
        }

        self.assertFalse(vdr_queue._high_priority_busy(exclude_task_id="current-vdr-task"))

    @patch("config.celery.app.control.inspect")
    @patch("ai_orchestrator.services.celery_queue_snapshot.CeleryQueueSnapshotService.snapshot")
    def test_different_high_priority_delivery_still_blocks_vdr(self, snapshot, inspect):
        snapshot.return_value = {
            "queues": [{"name": "high_priority", "ready_count": 0}],
            "unacked": {"messages": [{
                "queue": "high_priority",
                "task_id": "interactive-task",
            }]},
        }

        self.assertTrue(vdr_queue._high_priority_busy(exclude_task_id="current-vdr-task"))
        inspect.assert_not_called()

    @patch("config.celery.app.control.inspect")
    @patch("ai_orchestrator.services.celery_queue_snapshot.CeleryQueueSnapshotService.snapshot")
    def test_active_interactive_delivery_still_blocks_vdr(self, snapshot, inspect):
        snapshot.return_value = {
            "queues": [{"name": "high_priority", "ready_count": 0}],
            "unacked": {"messages": []},
        }
        inspect.return_value.active.return_value = {
            "worker": [{
                "id": "interactive-task",
                "delivery_info": {"routing_key": "high_priority"},
            }],
        }

        self.assertTrue(vdr_queue._high_priority_busy(exclude_task_id="current-vdr-task"))

    @patch("config.celery.app.control.inspect")
    @patch("ai_orchestrator.services.celery_queue_snapshot.CeleryQueueSnapshotService.snapshot")
    def test_ready_interactive_delivery_still_blocks_vdr(self, snapshot, inspect):
        snapshot.return_value = {
            "queues": [{"name": "high_priority", "ready_count": 1}],
            "unacked": {"messages": []},
        }

        self.assertTrue(vdr_queue._high_priority_busy(exclude_task_id="current-vdr-task"))
        inspect.assert_not_called()

    @patch("ai_orchestrator.models.AIAuditLog.objects.filter")
    @patch("config.celery.app.control.inspect")
    @patch("ai_orchestrator.services.celery_queue_snapshot.CeleryQueueSnapshotService.snapshot")
    def test_stale_terminal_unacked_delivery_does_not_block_vdr(
        self,
        snapshot,
        inspect,
        audit_filter,
    ):
        snapshot.return_value = {
            "queues": [{"name": "high_priority", "ready_count": 0}],
            "unacked": {"messages": [{
                "queue": "high_priority",
                "task_id": "failed-chat-task",
                "age_seconds": 7200,
            }]},
        }
        audit_filter.return_value.exists.return_value = False
        inspect.return_value.active.return_value = {}

        self.assertFalse(vdr_queue._high_priority_busy(exclude_task_id="current-vdr-task"))
        audit_filter.assert_called_once_with(
            celery_task_id__in={"failed-chat-task"},
            status__in=vdr_queue.ACTIVE_STATUSES,
        )

    @patch("ai_orchestrator.models.AIAuditLog.objects.filter")
    @patch("config.celery.app.control.inspect")
    @patch("ai_orchestrator.services.celery_queue_snapshot.CeleryQueueSnapshotService.snapshot")
    def test_stale_unacked_delivery_with_live_audit_still_blocks_vdr(
        self,
        snapshot,
        inspect,
        audit_filter,
    ):
        snapshot.return_value = {
            "queues": [{"name": "high_priority", "ready_count": 0}],
            "unacked": {"messages": [{
                "queue": "high_priority",
                "task_id": "active-chat-task",
                "age_seconds": 7200,
            }]},
        }
        audit_filter.return_value.exists.return_value = True

        self.assertTrue(vdr_queue._high_priority_busy(exclude_task_id="current-vdr-task"))
        inspect.assert_not_called()

    def test_document_retry_keeps_non_interactive_queue(self):
        from deals.tasks import _document_retry_queue

        request = MagicMock(delivery_info={"routing_key": "low_priority"})
        self.assertEqual(
            _document_retry_queue(request, durable_delivery=False),
            "low_priority",
        )

        request.delivery_info = {"routing_key": "high_priority"}
        self.assertEqual(
            _document_retry_queue(request, durable_delivery=True),
            "vdr_work",
        )


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

    def make_report_job(self, title="Report", sections=("Executive Summary",)):
        deal = Deal.objects.create(title=title)
        audit = AIAuditLog.objects.create(
            source_type="deal_full_synthesis", source_id=str(deal.id), context_label=title,
            model_used="model", system_prompt="queued", user_prompt="queued",
            status="PENDING", is_success=False,
            source_metadata=vdr_queue.initial_metadata(
                kind="report",
                manifest=[
                    {"position": position, "title": section, "status": "queued"}
                    for position, section in enumerate(sections, start=1)
                ],
            ),
        )
        return deal, audit

    @patch("deals.tasks.coordinate_vdr_queue.apply_async")
    def test_kick_publishes_coordinator_task(self, apply_async):
        self.assertTrue(vdr_queue.kick(countdown=3))
        apply_async.assert_called_once_with(queue="vdr_control", countdown=3)

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
    def test_yielded_document_releases_ownership_and_remains_queued(self, kick):
        _, audit = self.make_job(files=("a",))
        metadata = dict(audit.source_metadata)
        metadata.update({
            "queue_state": "active", "dispatch_generation": 2,
            "current_task_id": "document-task", "current_unit_type": "document",
            "current_unit_key": "a",
        })
        metadata["document_queue"][0].update({
            "status": "processing", "celery_task_id": "document-task",
        })
        audit.status = "PROCESSING"
        audit.source_metadata = metadata
        audit.save()

        self.assertTrue(vdr_queue.unit_finished(
            str(audit.id), task_id="document-task", generation=2, unit_key="a",
            result={"status": "yielded", "reason": "Report work is waiting."},
        ))

        audit.refresh_from_db()
        item = audit.source_metadata["document_queue"][0]
        self.assertEqual(item["status"], "queued")
        self.assertIsNone(item["celery_task_id"])
        self.assertNotIn("completed_at", item)
        self.assertEqual(audit.source_metadata["queue_state"], "waiting_next_unit")
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

    @patch("deals.services.vdr_queue.kick")
    @patch("config.celery.app.control.inspect")
    @patch("ai_orchestrator.services.celery_queue_snapshot.CeleryQueueSnapshotService.snapshot")
    @patch("deals.services.vdr_queue.cache.get")
    def test_reconcile_immediately_recovers_a_delivery_owned_by_replaced_deploy(
        self, cache_get, snapshot, inspect, kick,
    ):
        _, audit = self.make_job(files=("a",))
        metadata = dict(audit.source_metadata)
        metadata.update({
            "queue_state": "active", "dispatch_generation": 2,
            "current_task_id": "old-task", "current_unit_type": "document",
            "current_unit_key": "a", "worker_instance_id": "old-deploy",
            "heartbeat_at": timezone.now().isoformat(), "recovery_count": 2,
        })
        metadata["document_queue"][0]["status"] = "processing"
        audit.status = "PROCESSING"
        audit.source_metadata = metadata
        audit.save()
        cache_get.return_value = {"instance_id": "new-deploy"}
        snapshot.return_value = {"messages": [], "unacked": {"messages": []}}
        inspect.return_value.active.return_value = {}
        inspect.return_value.reserved.return_value = {}

        result = vdr_queue.reconcile()

        audit.refresh_from_db()
        self.assertEqual(result["recovered"], 1)
        self.assertEqual(audit.source_metadata["queue_state"], "recovering")
        self.assertEqual(audit.source_metadata["recovery_count"], 2)
        self.assertEqual(audit.source_metadata["deployment_recovery_count"], 1)
        self.assertIsNone(audit.source_metadata["worker_instance_id"])

    @patch("deals.services.vdr_queue._high_priority_busy", return_value=False)
    @patch("deals.tasks.process_single_document_async.apply_async")
    def test_dispatch_clears_previous_worker_ownership(self, apply_async, _busy):
        _, audit = self.make_job(files=("a",))
        audit.source_metadata = {**audit.source_metadata, "worker_instance_id": "old-deploy"}
        audit.save()

        with self.captureOnCommitCallbacks(execute=True):
            vdr_queue.dispatch()

        audit.refresh_from_db()
        self.assertIsNone(audit.source_metadata["worker_instance_id"])

    def test_queue_position_puts_reports_before_indexing_jobs(self):
        _, first = self.make_job("Indexing", files=("a",))
        _, second = self.make_report_job()
        self.assertEqual(vdr_queue.queue_position(str(second.id)), 1)
        self.assertEqual(vdr_queue.queue_position(str(first.id)), 2)

    @patch("deals.services.vdr_queue._high_priority_busy", return_value=False)
    @patch("deals.tasks.process_vdr_report_section.apply_async")
    def test_new_report_dispatches_before_waiting_indexing_job(self, apply_async, _busy):
        _, indexing = self.make_job("Indexing", files=("a",))
        indexing.status = "PROCESSING"
        indexing.source_metadata = {
            **indexing.source_metadata,
            "queue_state": "waiting_next_unit",
        }
        indexing.save()
        _, report = self.make_report_job()

        with self.captureOnCommitCallbacks(execute=True):
            result = vdr_queue.dispatch()

        report.refresh_from_db()
        indexing.refresh_from_db()
        self.assertEqual(result["audit_log_id"], str(report.id))
        self.assertEqual(result["unit"], "Executive Summary")
        self.assertEqual(report.source_metadata["queue_state"], "dispatching")
        self.assertEqual(indexing.source_metadata["queue_state"], "waiting_next_unit")
        apply_async.assert_called_once()

    @patch("deals.tasks.process_vdr_report_section.apply_async")
    def test_report_waits_for_current_document_segment_boundary(self, apply_async):
        _, indexing = self.make_job("Indexing", files=("a",))
        indexing.status = "PROCESSING"
        indexing.source_metadata = {
            **indexing.source_metadata,
            "queue_state": "active",
            "current_task_id": "document-task",
            "current_unit_type": "document",
            "current_unit_key": "a",
        }
        indexing.source_metadata["document_queue"][0].update({
            "status": "processing", "celery_task_id": "document-task",
        })
        indexing.save()
        self.make_report_job()

        result = vdr_queue.dispatch()

        self.assertEqual(result, {
            "status": "active",
            "audit_log_id": str(indexing.id),
        })
        apply_async.assert_not_called()

    def test_document_boundary_detects_waiting_report(self):
        _, report = self.make_report_job()
        self.assertTrue(vdr_queue.report_work_waiting())
        self.assertFalse(vdr_queue.report_work_waiting(exclude_audit_log_id=str(report.id)))

    @patch("deals.services.vdr_queue._high_priority_busy", return_value=False)
    @patch("deals.tasks.process_vdr_report_section.apply_async")
    def test_report_dispatches_one_section_on_the_same_lane(self, apply_async, _busy):
        _, audit = self.make_report_job(sections=("First section", "Second section"))
        with self.captureOnCommitCallbacks(execute=True):
            result = vdr_queue.dispatch()
        audit.refresh_from_db()
        self.assertEqual(result["unit"], "First section")
        self.assertEqual(audit.source_metadata["report_section_queue"][0]["status"], "processing")
        self.assertEqual(audit.source_metadata["report_section_queue"][1]["status"], "queued")
        apply_async.assert_called_once()
