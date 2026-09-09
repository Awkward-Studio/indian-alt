"""Distributed lease for the single-slot text inference server."""

from __future__ import annotations

import time
import threading
import uuid
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone


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
                current = cache.get(self.KEY)
                if not isinstance(current, dict) or current.get("lease_token") != self.owner.get("lease_token"):
                    return
                cache.set(self.KEY, self.owner, timeout=self.lease_ttl)
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
            try:
                acquired = cache.add(self.KEY, self.owner, timeout=self.lease_ttl)
                if acquired:
                    self.acquired = True
                    acquired_at = timezone.now()
                    self._update_audit(
                        inference_state="active",
                        inference_started_at=acquired_at.isoformat(),
                        inference_queue_wait_ms=max(
                            0, int((acquired_at - self.wait_started_at).total_seconds() * 1000)
                        ),
                        inference_lease_owner=self.owner,
                    )
                    self._start_heartbeat()
                    return self
                # A false cache result with no visible owner means Redis may
                # be unavailable. Waiting is safer than sending a second
                # request to a single-slot model server.
                if cache.get(self.KEY) is None:
                    self._update_audit(inference_queue_unavailable=True)
            except Exception as exc:
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
                current = cache.get(self.KEY)
                if isinstance(current, dict) and current.get("lease_token") == self.owner.get("lease_token"):
                    cache.delete(self.KEY)
            except Exception:
                pass
        self._update_audit(
            inference_state="released",
            inference_released_at=timezone.now().isoformat(),
        )
