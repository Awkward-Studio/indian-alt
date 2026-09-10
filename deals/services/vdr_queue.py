"""Database-authoritative FIFO coordination for VDR indexing and reports."""
from __future__ import annotations

import hashlib
import threading
import uuid
import time
from datetime import datetime, timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import connection, transaction
from django.utils import timezone


QUEUE_VERSION = 2
ACTIVE_STATUSES = ("PENDING", "PROCESSING")
TERMINAL_STATES = {"completed", "failed", "cancelled"}
_sqlite_lock = threading.Lock()


def enabled() -> bool:
    return bool(getattr(settings, "VDR_DURABLE_QUEUE_ENABLED", False))


def _lock_coordinator() -> bool:
    """Take one transaction-scoped coordinator lock across all API instances."""
    if connection.vendor != "postgresql":
        return _sqlite_lock.acquire(blocking=False)
    # Stable signed 63-bit key derived from the queue name.
    key = int.from_bytes(hashlib.sha256(b"india-alternatives:vdr:v2").digest()[:8], "big") & ((1 << 63) - 1)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_xact_lock(%s)", [key])
        return bool(cursor.fetchone()[0])


def _unlock_fallback() -> None:
    if connection.vendor != "postgresql" and _sqlite_lock.locked():
        _sqlite_lock.release()


def initial_metadata(*, kind: str, manifest: list[dict], **extra) -> dict:
    now = timezone.now().isoformat()
    key = "document_queue" if kind == "indexing" else "report_section_queue"
    return {
        "queue_version": QUEUE_VERSION,
        "queue_scope": "vdr",
        "queue_kind": kind,
        "queue_name": "vdr_work",
        "queue_state": "queued",
        "queued_at": now,
        "dispatch_generation": 0,
        "current_task_id": None,
        "current_unit_type": None,
        "current_unit_key": None,
        "worker_instance_id": None,
        "heartbeat_at": None,
        "recovery_count": 0,
        "max_recoveries": int(getattr(settings, "VDR_MAX_RECOVERIES", 3)),
        key: manifest,
        **extra,
    }


def queue_position(audit_log_id: str) -> int | None:
    from ai_orchestrator.models import AIAuditLog

    target = AIAuditLog.objects.filter(id=audit_log_id).values("created_at", "source_metadata").first()
    if not target:
        return None
    metadata = target["source_metadata"] or {}
    if metadata.get("queue_version") != QUEUE_VERSION or metadata.get("queue_state") != "queued":
        return None
    return 1 + AIAuditLog.objects.filter(
        status__in=ACTIVE_STATUSES,
        created_at__lt=target["created_at"],
        source_metadata__queue_version=QUEUE_VERSION,
        source_metadata__queue_scope="vdr",
        source_metadata__queue_state="queued",
    ).count()


def kick(*, countdown: int = 0) -> bool:
    """Best-effort wake-up. The periodic beat task is the durable fallback."""
    if not enabled():
        return False
    try:
        from deals.tasks import coordinate_vdr_queue
        coordinate_vdr_queue.apply_async(queue="vdr_control", countdown=countdown)
        return True
    except Exception:
        return False


def _broadcast(audit_log_id: str, *, done: bool = False) -> None:
    try:
        from ai_orchestrator.models import AIAuditLog
        from ai_orchestrator.services.realtime import broadcast_audit_log_update
        audit = AIAuditLog.objects.filter(id=audit_log_id).first()
        if audit:
            broadcast_audit_log_update(audit, event_type="terminal" if done else "progress", done=done)
    except Exception:
        pass


def _next_unit(metadata: dict) -> tuple[str, str, dict] | None:
    if metadata.get("queue_kind") == "indexing":
        for item in metadata.get("document_queue") or []:
            if item.get("status") in {"queued", "retrying", "recovering"}:
                return "document", str(item.get("source_file_id") or ""), item
    else:
        for item in metadata.get("report_section_queue") or []:
            if item.get("status") in {"queued", "retrying", "recovering"}:
                return "section", str(item.get("title") or ""), item
    return None


def _high_priority_busy(*, exclude_task_id: str | None = None) -> bool:
    """Report whether interactive work is queued or currently running."""
    excluded = str(exclude_task_id or "")

    def is_other_task(task: dict, id_key: str) -> bool:
        return not excluded or str(task.get(id_key) or "") != excluded

    try:
        from config.celery import app as celery_app
        from ai_orchestrator.services.celery_queue_snapshot import CeleryQueueSnapshotService
        snapshot = CeleryQueueSnapshotService.snapshot(celery_app, queues=("high_priority",))
        if sum(item["ready_count"] for item in snapshot["queues"]):
            return True
        if any(
            item.get("queue") == "high_priority" and is_other_task(item, "task_id")
            for item in snapshot.get("unacked", {}).get("messages", [])
        ):
            return True
        inspector = celery_app.control.inspect(timeout=0.5)
        for tasks in (inspector.active() or {}).values():
            if any(
                (task.get("delivery_info") or {}).get("routing_key") == "high_priority"
                and is_other_task(task, "id")
                for task in tasks or []
            ):
                return True
    except Exception:
        pass
    return False


def interactive_work_waiting(*, exclude_task_id: str | None = None) -> bool:
    """Public segment-boundary check used by long-running VDR document tasks."""
    return _high_priority_busy(exclude_task_id=exclude_task_id)


def dispatch() -> dict:
    """Dispatch at most one unit from the oldest eligible durable VDR job."""
    from ai_orchestrator.models import AIAuditLog
    from deals.tasks import process_single_document_async, process_vdr_report_section, vdr_unit_completed

    fallback_locked = False
    try:
        with transaction.atomic():
            if not _lock_coordinator():
                return {"status": "locked"}
            fallback_locked = connection.vendor != "postgresql"
            active = AIAuditLog.objects.select_for_update().filter(
                status="PROCESSING",
                source_metadata__queue_version=QUEUE_VERSION,
                source_metadata__queue_scope="vdr",
                source_metadata__queue_state__in=["dispatching", "active", "waiting_next_unit", "recovering"],
            ).order_by("created_at").first()
            audit = active or AIAuditLog.objects.select_for_update().filter(
                status="PENDING",
                source_metadata__queue_version=QUEUE_VERSION,
                source_metadata__queue_scope="vdr",
                source_metadata__queue_state="queued",
            ).order_by("created_at").first()
            if not audit:
                return {"status": "idle"}
            metadata = dict(audit.source_metadata or {})
            if metadata.get("queue_state") in {"dispatching", "active"} and metadata.get("current_task_id"):
                return {"status": "active", "audit_log_id": str(audit.id)}
            if _high_priority_busy():
                transaction.on_commit(lambda: kick(countdown=5))
                return {"status": "deferred_for_interactive_work"}
            unit = _next_unit(metadata)
            if not unit:
                transaction.on_commit(lambda: _finish_job(str(audit.id)))
                return {"status": "finishing", "audit_log_id": str(audit.id)}
            unit_type, unit_key, item = unit
            generation = int(metadata.get("dispatch_generation") or 0) + 1
            task_id = str(uuid.uuid4())
            now = timezone.now().isoformat()
            item["status"] = "processing"
            item["celery_task_id"] = task_id
            item["started_at"] = now
            metadata.update({
                "queue_state": "dispatching",
                "dispatch_generation": generation,
                "current_task_id": task_id,
                "current_unit_type": unit_type,
                "current_unit_key": unit_key,
                "heartbeat_at": now,
            })
            audit.status = "PROCESSING"
            audit.celery_task_id = task_id
            audit.source_metadata = metadata
            audit.save(update_fields=["status", "celery_task_id", "source_metadata"])
            transaction.on_commit(lambda: _broadcast(str(audit.id)))

            if unit_type == "document":
                kwargs = {
                    "file_info": item["file_info"],
                    "deal_id": str(audit.source_id),
                    "user_email": metadata["user_email"],
                    "is_preview": False,
                    "audit_log_id": str(audit.id),
                    "resume_artifact_run_id": item.get("resume_artifact_run_id"),
                    "force_fresh": bool(metadata.get("force_fresh")),
                    "queue_generation": generation,
                    "queue_unit_key": unit_key,
                }
                transaction.on_commit(lambda: process_single_document_async.apply_async(
                    kwargs=kwargs, queue="vdr_work", task_id=task_id,
                    link=vdr_unit_completed.s(str(audit.id), task_id, generation, unit_key),
                ))
            else:
                transaction.on_commit(lambda: process_vdr_report_section.apply_async(
                    kwargs={
                        "deal_id": str(audit.source_id), "audit_log_id": str(audit.id),
                        "section_title": unit_key, "queue_generation": generation,
                    }, queue="vdr_work", task_id=task_id,
                    link=vdr_unit_completed.s(str(audit.id), task_id, generation, unit_key),
                ))
            return {"status": "dispatched", "audit_log_id": str(audit.id), "unit": unit_key}
    finally:
        if fallback_locked:
            _unlock_fallback()


def delivery_is_current(audit_log_id: str, *, task_id: str, generation: int, unit_key: str) -> bool:
    from ai_orchestrator.models import AIAuditLog
    row = AIAuditLog.objects.filter(id=audit_log_id, status="PROCESSING").values("source_metadata").first()
    metadata = (row or {}).get("source_metadata") or {}
    return (
        metadata.get("queue_version") == QUEUE_VERSION
        and str(metadata.get("current_task_id") or "") == str(task_id)
        and int(metadata.get("dispatch_generation") or 0) == int(generation)
        and str(metadata.get("current_unit_key") or "") == str(unit_key)
        and not metadata.get("cancel_requested")
    )


def heartbeat(audit_log_id: str, *, task_id: str, generation: int, unit_key: str, worker_id: str | None = None) -> bool:
    from ai_orchestrator.models import AIAuditLog
    with transaction.atomic():
        audit = AIAuditLog.objects.select_for_update().filter(id=audit_log_id).first()
        if not audit:
            return False
        metadata = dict(audit.source_metadata or {})
        if not delivery_is_current(audit_log_id, task_id=task_id, generation=generation, unit_key=unit_key):
            return False
        metadata.update({"queue_state": "active", "heartbeat_at": timezone.now().isoformat()})
        if worker_id:
            metadata["worker_instance_id"] = worker_id
        audit.source_metadata = metadata
        audit.save(update_fields=["source_metadata"])
        return True


def start_heartbeat(audit_log_id: str, *, task_id: str, generation: int, unit_key: str, worker_id: str | None = None) -> None:
    """Renew parent ownership until the delivery completes or is superseded."""
    def pulse() -> None:
        from django.db import close_old_connections
        interval = int(getattr(settings, "VDR_WORKER_HEARTBEAT_SECONDS", 30))
        while True:
            time.sleep(interval)
            close_old_connections()
            try:
                if not heartbeat(
                    audit_log_id, task_id=task_id, generation=generation,
                    unit_key=unit_key, worker_id=worker_id,
                ):
                    return
            except Exception:
                close_old_connections()

    threading.Thread(target=pulse, daemon=True, name=f"vdr-heartbeat-{task_id[:8]}").start()


def unit_finished(audit_log_id: str, *, task_id: str, generation: int, unit_key: str, result: dict) -> bool:
    """Persist a unit result and clear ownership before waking the coordinator."""
    from ai_orchestrator.models import AIAuditLog
    with transaction.atomic():
        audit = AIAuditLog.objects.select_for_update().filter(id=audit_log_id).first()
        if not audit:
            return False
        metadata = dict(audit.source_metadata or {})
        if not (
            str(metadata.get("current_task_id") or "") == str(task_id)
            and int(metadata.get("dispatch_generation") or 0) == int(generation)
            and str(metadata.get("current_unit_key") or "") == str(unit_key)
        ):
            return False
        manifest_key = "document_queue" if metadata.get("queue_kind") == "indexing" else "report_section_queue"
        key_name = "source_file_id" if manifest_key == "document_queue" else "title"
        terminal = str(result.get("status") or "failed").lower()
        if terminal == "success":
            terminal = "completed"
        for item in metadata.get(manifest_key) or []:
            if str(item.get(key_name) or "") == str(unit_key):
                item.update({
                    "status": terminal,
                    "completed_at": timezone.now().isoformat(),
                    "error": result.get("error") or result.get("reason"),
                })
                if result.get("document_id"):
                    item["document_id"] = result["document_id"]
                if result.get("section"):
                    item["content"] = result["section"]
                if result.get("evidence_metadata"):
                    item["evidence_metadata"] = result["evidence_metadata"]
                break
        metadata.update({
            "queue_state": "waiting_next_unit",
            "current_task_id": None,
            "current_unit_type": None,
            "current_unit_key": None,
            "heartbeat_at": timezone.now().isoformat(),
        })
        audit.source_metadata = metadata
        audit.save(update_fields=["source_metadata"])
    kick()
    _broadcast(audit_log_id)
    return True


def _finish_job(audit_log_id: str) -> None:
    from ai_orchestrator.models import AIAuditLog
    from deals.tasks import assemble_vdr_report

    audit = AIAuditLog.objects.filter(id=audit_log_id).first()
    if not audit or audit.status not in ACTIVE_STATUSES:
        return
    metadata = audit.source_metadata or {}
    manifest = metadata.get("document_queue" if metadata.get("queue_kind") == "indexing" else "report_section_queue") or []
    failed = [item for item in manifest if item.get("status") in {"failed", "cancelled"}]
    if metadata.get("queue_kind") == "report" and not failed:
        try:
            assemble_vdr_report(audit_log_id)
            _broadcast(audit_log_id, done=True)
        except Exception as exc:
            audit.status = "FAILED"
            audit.is_success = False
            audit.completed_at = timezone.now()
            audit.error_message = str(exc)
            audit.source_metadata = {**metadata, "queue_state": "failed"}
            audit.save(update_fields=["status", "is_success", "completed_at", "error_message", "source_metadata"])
            _broadcast(audit_log_id, done=True)
        kick()
        return
    remaining = []
    if metadata.get("queue_kind") == "indexing" and metadata.get("coverage_policy") == "selected_documents":
        from deals.services.document_artifacts import DocumentArtifactService
        from deals.models import DealDocument
        remaining = [
            document for document in DealDocument.objects.filter(deal_id=audit.source_id)
            if not DocumentArtifactService.artifact_complete(document)
        ]
    audit.status = "FAILED" if failed else "COMPLETED"
    audit.is_success = not failed
    audit.completed_at = timezone.now()
    audit.error_message = f"{len(failed)} VDR document(s) failed." if failed else None
    audit.source_metadata = {
        **metadata, "queue_state": "failed" if failed else "completed",
        "processed_count": sum(item.get("status") == "completed" for item in manifest),
        "cached_count": sum(item.get("status") == "cached" for item in manifest),
        "failed_count": len(failed), "remaining_document_count": len(remaining),
        "workflow_stage": "artifacts_ready" if not failed else "artifact_processing_failed",
        "analysis_confirmation_required": not failed and not remaining,
    }
    audit.save(update_fields=["status", "is_success", "completed_at", "error_message", "source_metadata"])
    from deals.models import Deal
    Deal.objects.filter(id=audit.source_id).update(
        processing_status="failed" if failed or remaining else "completed",
        processing_error=(
            audit.error_message if failed else
            f"Selected VDR run completed, but {len(remaining)} other document(s) still need indexing."
            if remaining else None
        ),
    )
    _broadcast(audit_log_id, done=True)
    kick()


def reconcile() -> dict:
    """Recover abandoned ownership and ensure queued DB work has a dispatcher."""
    from ai_orchestrator.models import AIAuditLog
    now = timezone.now()
    recovered = 0
    failed = 0
    observed_task_ids: set[str] = set()
    inspection_available = False
    try:
        from config.celery import app as celery_app
        from ai_orchestrator.services.celery_queue_snapshot import CeleryQueueSnapshotService
        snapshot = CeleryQueueSnapshotService.snapshot(celery_app)
        observed_task_ids.update(str(row.get("task_id")) for row in [
            *snapshot.get("messages", []), *snapshot.get("unacked", {}).get("messages", []),
        ] if row.get("task_id"))
        inspector = celery_app.control.inspect(timeout=1)
        active_payload = inspector.active()
        reserved_payload = inspector.reserved()
        for payload in [active_payload or {}, reserved_payload or {}]:
            for tasks in payload.values():
                observed_task_ids.update(str((task.get("request") or task).get("id")) for task in tasks or [])
        inspection_available = not snapshot.get("warning") and active_payload is not None and reserved_payload is not None
    except Exception:
        pass
    candidates = AIAuditLog.objects.filter(
        status="PROCESSING",
        source_metadata__queue_version=QUEUE_VERSION,
        source_metadata__queue_scope="vdr",
        source_metadata__queue_state__in=["dispatching", "active", "recovering"],
    )
    for candidate in candidates:
        metadata = candidate.source_metadata or {}
        current_worker = cache.get("vdr:presence:worker:current")
        current_worker_id = current_worker.get("instance_id") if isinstance(current_worker, dict) else None
        owner_worker_id = metadata.get("worker_instance_id")
        heartbeat_at = metadata.get("heartbeat_at")
        try:
            heartbeat_time = datetime.fromisoformat(heartbeat_at) if heartbeat_at else candidate.created_at
        except (TypeError, ValueError):
            heartbeat_time = candidate.created_at
        if timezone.is_naive(heartbeat_time):
            heartbeat_time = timezone.make_aware(heartbeat_time)
        age = now - heartbeat_time
        hard_stale = age >= timedelta(seconds=int(getattr(settings, "VDR_HARD_STALE_SECONDS", 1800)))
        worker_replaced = bool(owner_worker_id and owner_worker_id != current_worker_id)
        missing_after_grace = (
            inspection_available
            and age >= timedelta(seconds=int(getattr(settings, "VDR_STALE_SECONDS", 300)))
            and str(metadata.get("current_task_id") or "") not in observed_task_ids
        )
        if not hard_stale and not missing_after_grace and not worker_replaced:
            continue
        with transaction.atomic():
            audit = AIAuditLog.objects.select_for_update().get(id=candidate.id)
            metadata = dict(audit.source_metadata or {})
            recoveries = int(metadata.get("recovery_count") or 0) + 1
            if recoveries > int(metadata.get("max_recoveries") or 3):
                audit.status = "FAILED"
                audit.is_success = False
                audit.error_message = "VDR job exceeded its automatic recovery limit."
                audit.completed_at = now
                metadata["queue_state"] = "failed"
                metadata.update({"current_task_id": None, "current_unit_type": None, "current_unit_key": None})
                failed += 1
            else:
                manifest_key = "document_queue" if metadata.get("queue_kind") == "indexing" else "report_section_queue"
                key_name = "source_file_id" if manifest_key == "document_queue" else "title"
                for item in metadata.get(manifest_key) or []:
                    if str(item.get(key_name) or "") == str(metadata.get("current_unit_key") or ""):
                        item["status"] = "recovering"
                        item["celery_task_id"] = None
                        break
                metadata.update({
                    "queue_state": "recovering", "recovery_count": recoveries,
                    "dispatch_generation": int(metadata.get("dispatch_generation") or 0) + 1,
                    "current_task_id": None, "current_unit_type": None, "current_unit_key": None,
                    "heartbeat_at": now.isoformat(),
                })
                recovered += 1
            audit.source_metadata = metadata
            audit.save()
            if audit.status == "FAILED" and metadata.get("queue_kind") == "indexing":
                from deals.models import Deal
                Deal.objects.filter(id=audit.source_id).update(
                    processing_status="failed", processing_error=audit.error_message,
                )
            transaction.on_commit(lambda audit_id=str(audit.id), done=audit.status == "FAILED": _broadcast(
                audit_id, done=done,
            ))
    kick()
    return {"recovered": recovered, "failed": failed}
