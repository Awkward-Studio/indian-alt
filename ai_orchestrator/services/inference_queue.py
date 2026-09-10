"""Distributed lease for the single-slot text inference server."""

from __future__ import annotations

import time
import threading
import uuid
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone


class InferenceCancelled(RuntimeError):
    pass


class InferenceQueueLease:
    """Serialize model requests and persist queue ownership on the audit row."""

    KEY = "ai:inference:lease:v1"
    DEFAULT_LEASE_TTL = 3600

    def __init__(self, audit_log, *, max_wait_seconds: int | float = 0) -> None:
        self.audit_log = audit_log
        self.max_wait_seconds = max(0, float(max_wait_seconds or 0))
        self.poll_seconds = max(
            0.5, float(getattr(settings, "AI_INFERENCE_QUEUE_POLL_SECONDS", 2.0) or 2.0)
        )
        self.lease_ttl = max(
            300,
            int(
                getattr(
                    settings,
                    "AI_INFERENCE_QUEUE_LEASE_TTL",
                    self.DEFAULT_LEASE_TTL,
                )
                or self.DEFAULT_LEASE_TTL
            ),
        )
        self.owner: dict[str, Any] = {
            "audit_log_id": str(audit_log.id),
            "celery_task_id": str(audit_log.celery_task_id or ""),
            "source_type": audit_log.source_type,
            "source_id": str(audit_log.source_id or ""),
            "context_label": audit_log.context_label or "",
            "lease_token": uuid.uuid4().hex,
        }
        self.acquired = False
        self.wait_started_at = timezone.now()
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None

    def check_cancelled(self):
        from ai_orchestrator.models import AIAuditLog
        ids = [self.audit_log.id]
        parent = (self.audit_log.source_metadata or {}).get("vdr_parent_audit_id")
        if parent:
            ids.append(parent)
        rows = list(AIAuditLog.objects.filter(id__in=ids).values("status", "source_metadata"))
        if len(rows) != len(set(str(value) for value in ids)) or any(
            row["status"] not in {"PENDING", "PROCESSING"}
            or (row["source_metadata"] or {}).get("cancel_requested") for row in rows
        ):
            raise InferenceCancelled("Inference workflow was cancelled or is already terminal.")
        expected_generation = (self.audit_log.source_metadata or {}).get("vdr_dispatch_generation")
        if parent and expected_generation is not None:
            parent_row = next(
                (row for row in rows if int((row["source_metadata"] or {}).get("queue_version") or 0) == 2),
                None,
            )
            if (
                parent_row is None
                or int((parent_row["source_metadata"] or {}).get("dispatch_generation") or 0)
                != int(expected_generation)
            ):
                raise InferenceCancelled("Inference workflow was superseded by a recovered VDR delivery.")

    def record_slot_progress(self, **updates):
        self.check_cancelled()
        current = cache.get(self.KEY)
        if not isinstance(current, dict) or current.get("lease_token") != self.owner["lease_token"]:
            raise InferenceCancelled("Inference lease ownership was lost; closing the model request.")
        if updates.get("inference_state") == "processing" and not (self.audit_log.source_metadata or {}).get("inference_started_at"):
            updates["inference_started_at"] = timezone.now().isoformat()
        self._update_audit(**updates)

    def _mutate_owned_lease(self, *, renew=False, owner=None):
        # Compare the serialized owner and mutate in one Redis operation.
        # A get/delete or get/set pair can otherwise erase a successor's lease.
        owner = self.owner if owner is None else owner
        backend = getattr(cache, "client", None)
        if backend is not None:
            client = backend.get_client(write=True)
            script = (
                "if redis.call('get', KEYS[1]) == ARGV[1] then "
                + ("return redis.call('expire', KEYS[1], ARGV[2]) " if renew else "return redis.call('del', KEYS[1]) ")
                + "else return 0 end"
            )
            return bool(client.eval(script, 1, cache.make_key(self.KEY), backend.encode(owner), self.lease_ttl))
        # Local-memory cache is used by local tests, never distributed workers.
        current = cache.get(self.KEY)
        if isinstance(current, dict) and current.get("lease_token") == owner["lease_token"]:
            return cache.touch(self.KEY, self.lease_ttl) if renew else cache.delete(self.KEY)
        return False

    def _update_audit(self, **updates: Any) -> None:
        metadata = dict(self.audit_log.source_metadata or {})
        metadata.update(updates)
        self.audit_log.source_metadata = metadata
        self.audit_log.save(update_fields=["source_metadata"])

    def _heartbeat(self) -> None:
        """Keep a live request from losing the lease during a long decode."""
        interval = max(15.0, min(60.0, self.lease_ttl / 3))
        while not self._heartbeat_stop.wait(interval):
            try:
                if not self._mutate_owned_lease(renew=True):
                    return
            except Exception:
                # The lease expiry remains the safety valve if Redis is briefly
                # unavailable. Never replace another worker's lease here.
                return

    def _start_heartbeat(self) -> None:
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat,
            name=f"inference-lease-{self.owner['lease_token'][:8]}",
            daemon=True,
        )
        self._heartbeat_thread.start()

    def _stop_heartbeat(self) -> None:
        self._heartbeat_stop.set()
        if self._heartbeat_thread and self._heartbeat_thread is not threading.current_thread():
            self._heartbeat_thread.join(timeout=2)

    @classmethod
    def force_release(cls) -> bool:
        """Release a lease during an administrator queue reset."""
        try:
            if cache.get(cls.KEY) is None:
                return True
            return bool(cache.delete(cls.KEY))
        except Exception:
            return False

    def __enter__(self) -> "InferenceQueueLease":
        self.wait_started_at = timezone.now()
        self._update_audit(
            inference_state="queued",
            inference_queue_entered_at=self.wait_started_at.isoformat(),
            inference_queue_lease_key=self.KEY,
        )
        deadline = (
            time.monotonic() + self.max_wait_seconds
            if self.max_wait_seconds
            else None
        )
        while True:
            self.check_cancelled()
            try:
                acquired = cache.add(self.KEY, self.owner, timeout=self.lease_ttl)
                if acquired:
                    self.acquired = True
                    acquired_at = timezone.now()
                    try:
                        self._update_audit(
                            inference_state="lease_acquired",
                            inference_lease_acquired_at=acquired_at.isoformat(),
                            inference_queue_wait_ms=max(
                                0, int((acquired_at - self.wait_started_at).total_seconds() * 1000)
                            ),
                            inference_lease_owner=self.owner,
                        )
                        self._start_heartbeat()
                    except Exception:
                        self._mutate_owned_lease()
                        raise
                    return self
                # A false cache result with no visible owner means Redis may
                # be unavailable. Waiting is safer than sending a second
                # request to a single-slot model server.
                if cache.get(self.KEY) is None:
                    self._update_audit(inference_queue_unavailable=True)
                elif self.audit_log.source_type == "document_evidence_segment":
                    from ai_orchestrator.models import AIAuditLog
                    owner = cache.get(self.KEY)
                    if isinstance(owner, dict) and owner.get("audit_log_id"):
                        live = AIAuditLog.objects.filter(
                            pk=owner["audit_log_id"], status__in=["PENDING", "PROCESSING"],
                        ).exists()
                        if not live:
                            # Segment transport also waits for an idle VM slot
                            # before posting, so a cancelled remote decode is
                            # allowed to drain even after this lease is removed.
                            self._mutate_owned_lease(owner=owner)
            except Exception as exc:
                if self.acquired:
                    raise
                self._update_audit(
                    inference_queue_unavailable=True,
                    inference_queue_error=str(exc)[:500],
                )

            if deadline is not None and time.monotonic() >= deadline:
                self._update_audit(
                    inference_state="failed",
                    inference_queue_timeout=True,
                    inference_queue_wait_ms=max(
                        0, int((timezone.now() - self.wait_started_at).total_seconds() * 1000)
                    ),
                )
                raise TimeoutError("Timed out waiting for the text inference queue.")
            time.sleep(self.poll_seconds)

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self._stop_heartbeat()
        if self.acquired:
            try:
                self._mutate_owned_lease()
            except Exception:
                pass
        self._update_audit(
            inference_state="released",
            inference_released_at=timezone.now().isoformat(),
        )
