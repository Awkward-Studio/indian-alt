"""Distributed lease for the single-slot text inference server."""

from __future__ import annotations

import time
import uuid
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone


class InferenceQueueLease:
    """Serialize model requests and persist queue ownership on the audit row."""

    KEY = "ai:inference:lease:v1"

    def __init__(self, audit_log, *, max_wait_seconds: int | float = 0) -> None:
        self.audit_log = audit_log
        self.max_wait_seconds = max(0, float(max_wait_seconds or 0))
        self.poll_seconds = max(
            0.5, float(getattr(settings, "AI_INFERENCE_QUEUE_POLL_SECONDS", 2.0) or 2.0)
        )
        self.lease_ttl = max(
            300, int(getattr(settings, "AI_INFERENCE_QUEUE_LEASE_TTL", 7200) or 7200)
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

    def _update_audit(self, **updates: Any) -> None:
        metadata = dict(self.audit_log.source_metadata or {})
        metadata.update(updates)
        self.audit_log.source_metadata = metadata
        self.audit_log.save(update_fields=["source_metadata"])

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
                    return self
                # django-redis may suppress connection errors and return
                # False. A missing owner distinguishes that from a real lock
                # and lets the request proceed instead of waiting forever.
                if cache.get(self.KEY) is None:
                    self._update_audit(
                        inference_state="active",
                        inference_queue_unavailable=True,
                        inference_started_at=timezone.now().isoformat(),
                    )
                    return self
            except Exception:
                self._update_audit(
                    inference_state="active",
                    inference_queue_unavailable=True,
                    inference_started_at=timezone.now().isoformat(),
                )
                return self

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
