import base64
import json
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from ai_orchestrator.models import AIAuditLog
from ai_orchestrator.services.celery_queue_snapshot import CeleryQueueSnapshotService
from deals.models import Deal, DealDocument


class CeleryQueueSnapshotServiceTests(SimpleTestCase):
    def test_snapshot_decodes_safe_vdr_context_in_redis_dispatch_order(self):
        def message(task_id, deal_id, name):
            body = json.dumps([
                [],
                {
                    "deal_id": deal_id,
                    "file_tree_map": [{"id": f"file-{task_id}", "name": name}],
                    "user_email": "private@example.test",
                    "force_fresh": True,
                },
                {},
            ]).encode()
            return json.dumps({
                "body": base64.b64encode(body).decode(),
                "content-encoding": "utf-8",
                "headers": {
                    "id": task_id,
                    "task": "deals.tasks.process_deal_folder_background",
                },
                "properties": {},
            }).encode()

        client = MagicMock()
        client.llen.side_effect = lambda key: 2 if key == "low_priority" else 0
        # Redis stores newest on the left. The service reverses this list.
        client.lrange.return_value = [
            message("newer", "deal-2", "Second.pdf"),
            message("older", "deal-1", "First.pdf"),
        ]
        channel = MagicMock(priority_steps=(0,), client=client)
        channel._q_for_pri.side_effect = lambda queue, _priority: queue
        connection = MagicMock()
        connection.channel.return_value = channel
        app = MagicMock()
        app.connection_for_read.return_value.__enter__.return_value = connection

        result = CeleryQueueSnapshotService.snapshot(app)

        low_priority = next(queue for queue in result["queues"] if queue["name"] == "low_priority")
        self.assertEqual(low_priority["ready_count"], 2)
        self.assertEqual([item["task_id"] for item in result["messages"]], ["older", "newer"])
        self.assertEqual(result["messages"][0]["deal_id"], "deal-1")
        self.assertEqual(result["messages"][0]["document_names"], ["First.pdf"])
        self.assertNotIn("user_email", result["messages"][0])


@override_settings(VLLM_BASE_URL="http://inference.test:8080/v1")
class QueueStatusEndpointTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("queue-admin", password="test-only", is_staff=True)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    @patch("ai_orchestrator.views.requests.get")
    @patch("ai_orchestrator.services.celery_queue_snapshot.CeleryQueueSnapshotService.snapshot")
    @patch("config.celery.app.control.inspect")
    def test_queue_status_joins_redis_deal_document_and_segment_state(
        self,
        inspect,
        snapshot,
        get_slots,
    ):
        inspect.return_value.active.return_value = {}
        inspect.return_value.reserved.return_value = {}
        inspect.return_value.scheduled.return_value = {}
        get_slots.return_value.json.return_value = []
        get_slots.return_value.raise_for_status.return_value = None

        deal = Deal.objects.create(title="Queue Deal", processing_status="processing")
        document = DealDocument.objects.create(
            deal=deal,
            title="Deck.pdf",
            onedrive_id="file-1",
        )
        parent = AIAuditLog.objects.create(
            source_type="vdr_indexing",
            source_id=str(deal.id),
            context_label="VDR indexing: Queue Deal",
            model_used="embed",
            system_prompt="queued",
            user_prompt="queued",
            status="PENDING",
            is_success=False,
            celery_task_id="root-task",
            source_metadata={
                "queue_name": "low_priority",
                "queued_at": "2026-09-10T03:00:00Z",
                "document_queue": [{
                    "source_file_id": "file-1",
                    "document_id": str(document.id),
                    "name": "Deck.pdf",
                    "status": "queued",
                    "celery_task_id": "document-task",
                }],
            },
        )
        segment = AIAuditLog.objects.create(
            source_type="document_evidence_segment",
            source_id=str(document.id),
            context_label="Document Evidence: Deck.pdf [1/3]",
            model_used="model",
            system_prompt="segment",
            user_prompt="segment",
            status="PROCESSING",
            is_success=False,
            celery_task_id="document-task",
            source_metadata={
                "vdr_parent_audit_id": str(parent.id),
                "segment_index": 0,
                "segment_count": 3,
                "inference_state": "processing",
            },
        )
        snapshot.return_value = {
            "queues": [
                {"name": "high_priority", "ready_count": 0, "shown_count": 0, "truncated": False},
                {"name": "default", "ready_count": 0, "shown_count": 0, "truncated": False},
                {"name": "low_priority", "ready_count": 1, "shown_count": 1, "truncated": False},
            ],
            "messages": [{
                "queue": "low_priority",
                "position": 1,
                "task_id": "root-task",
                "task_name": "deals.tasks.process_deal_folder_background",
                "display_name": "process deal folder background",
                "deal_id": str(deal.id),
            }],
            "warning": None,
        }

        response = self.client.get("/api/ai/history/queue-status/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["summary"]["redis_ready"], 1)
        self.assertEqual(response.data["vdr_runs"][0]["deal_title"], "Queue Deal")
        self.assertEqual(response.data["vdr_runs"][0]["queue_position"], 1)
        self.assertEqual(response.data["vdr_runs"][0]["documents"][0]["name"], "Deck.pdf")
        self.assertEqual(
            response.data["vdr_runs"][0]["documents"][0]["segments"][0]["audit_log_id"],
            str(segment.id),
        )
