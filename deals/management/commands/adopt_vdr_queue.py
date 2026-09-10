"""Adopt recoverable legacy VDR audits into the durable FIFO queue."""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand
from django.db import transaction

from ai_orchestrator.models import AIAuditLog
from deals.services import vdr_queue


VDR_TASK_NAMES = {
    "deals.tasks.process_deal_folder_background",
    "deals.tasks.process_single_document_async",
    "deals.tasks.finalize_folder_background",
    "deals.tasks.process_vdr_report_section",
}


class Command(BaseCommand):
    help = "Inspect or adopt active legacy VDR audit logs into queue_version=2."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Persist safe audit upgrades.")
        parser.add_argument("--json", action="store_true", help="Write machine-readable output.")

    def handle(self, *args, **options):
        rows = []
        candidates = AIAuditLog.objects.filter(
            source_type__in=["vdr_indexing", "deal_full_synthesis"],
            status__in=["PENDING", "PROCESSING"],
        ).order_by("created_at")
        for candidate in candidates:
            metadata = dict(candidate.source_metadata or {})
            if metadata.get("queue_version") == vdr_queue.QUEUE_VERSION:
                rows.append({"audit_log_id": str(candidate.id), "action": "already_durable"})
                continue
            kind = "report" if candidate.source_type == "deal_full_synthesis" else "indexing"
            manifest_key = "report_section_queue" if kind == "report" else "document_queue"
            manifest = metadata.get(manifest_key) or []
            if not manifest:
                rows.append({
                    "audit_log_id": str(candidate.id), "action": "skip",
                    "reason": f"No {manifest_key} checkpoint is available.",
                })
                continue
            rows.append({
                "audit_log_id": str(candidate.id), "action": "adopt" if options["apply"] else "would_adopt",
                "kind": kind, "preserved_units": len(manifest),
            })
            if options["apply"]:
                with transaction.atomic():
                    audit = AIAuditLog.objects.select_for_update().get(id=candidate.id)
                    current = dict(audit.source_metadata or {})
                    current.update(vdr_queue.initial_metadata(
                        kind=kind, manifest=current.get(manifest_key) or manifest,
                        **{key: value for key, value in current.items() if key not in {
                            "queue_version", "queue_scope", "queue_kind", "queue_name", "queue_state",
                            "queued_at", "dispatch_generation", "current_task_id", "current_unit_type",
                            "current_unit_key", "worker_instance_id", "heartbeat_at", "recovery_count",
                            "max_recoveries", manifest_key,
                        }},
                    ))
                    current["queue_state"] = "recovering"
                    current["recovery_count"] = 1
                    audit.status = "PROCESSING"
                    audit.celery_task_id = None
                    audit.source_metadata = current
                    audit.save(update_fields=["status", "celery_task_id", "source_metadata"])
        if options["apply"]:
            vdr_queue.kick()
        broker_rows = self._broker_candidates(apply=options["apply"])
        payload = {
            "mode": "apply" if options["apply"] else "dry-run",
            "audits": rows, "broker_messages": broker_rows,
        }
        lines = [
            *[f"{row['audit_log_id']}: {row['action']} {row.get('reason', '')}" for row in rows],
            *[f"{row.get('task_id')}: {row['action']} ({row['location']})" for row in broker_rows],
        ]
        self.stdout.write(json.dumps(payload, indent=2, default=str) if options["json"] else "\n".join(lines))

    @staticmethod
    def _broker_candidates(*, apply: bool) -> list[dict]:
        """Retire only exact, decoded VDR messages proven terminal or superseded."""
        from config.celery import app as celery_app
        from ai_orchestrator.services.celery_queue_snapshot import CeleryQueueSnapshotService

        rows = []

        def classification(decoded):
            if decoded.get("task_name") not in VDR_TASK_NAMES or not decoded.get("audit_log_id"):
                return None
            audit = AIAuditLog.objects.filter(id=decoded["audit_log_id"]).first()
            if not audit:
                return None
            if audit.status in {"COMPLETED", "FAILED"}:
                return "terminal"
            metadata = audit.source_metadata or {}
            if metadata.get("queue_version") == 2 and str(metadata.get("current_task_id") or "") != str(decoded.get("task_id") or ""):
                return "superseded"
            return None

        try:
            with celery_app.connection_for_read() as connection:
                channel = connection.channel()
                client = channel.client
                for queue_name in CeleryQueueSnapshotService.DEFAULT_QUEUES:
                    for priority in tuple(getattr(channel, "priority_steps", (0,))) or (0,):
                        key = channel._q_for_pri(queue_name, priority)
                        for raw in client.lrange(key, 0, -1):
                            try:
                                decoded = CeleryQueueSnapshotService._decode_message(raw, queue=queue_name, position=0)
                                reason = classification(decoded)
                                if not reason:
                                    continue
                                if apply:
                                    client.lrem(key, 1, raw)
                                rows.append({
                                    "task_id": decoded.get("task_id"), "location": "ready",
                                    "reason": reason, "action": "retired" if apply else "would_retire",
                                })
                            except Exception:
                                continue
                for delivery_tag, _ in client.zrange("unacked_index", 0, -1, withscores=True):
                    raw = client.hget("unacked", delivery_tag)
                    if not raw:
                        continue
                    try:
                        payload = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
                        envelope = payload[0] if isinstance(payload, list) else payload
                        decoded = CeleryQueueSnapshotService._decode_message(envelope, queue="unacked", position=0)
                        reason = classification(decoded)
                        if not reason:
                            continue
                        if apply:
                            client.hdel("unacked", delivery_tag)
                            client.zrem("unacked_index", delivery_tag)
                        rows.append({
                            "task_id": decoded.get("task_id"), "location": "unacked",
                            "reason": reason, "action": "retired" if apply else "would_retire",
                        })
                    except Exception:
                        continue
        except Exception as exc:
            rows.append({"task_id": None, "location": "broker", "action": "unavailable", "reason": str(exc)[:300]})
        return rows
