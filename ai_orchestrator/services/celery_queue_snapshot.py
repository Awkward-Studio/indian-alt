from __future__ import annotations

import base64
import json
from typing import Any


class CeleryQueueSnapshotService:
    """Read a safe, ordered snapshot directly from Celery's Redis queues."""

    DEFAULT_QUEUES = ("high_priority", "default", "low_priority")
    MAX_MESSAGES_PER_QUEUE = 500

    @classmethod
    def _decode_body(cls, envelope: dict) -> tuple[list, dict]:
        body = envelope.get("body")
        if isinstance(body, str):
            body = base64.b64decode(body)
        if isinstance(body, bytes):
            body = body.decode(envelope.get("content-encoding") or "utf-8")
        payload = json.loads(body) if isinstance(body, str) else body
        if not isinstance(payload, list) or len(payload) < 2:
            return [], {}
        args = payload[0] if isinstance(payload[0], list) else []
        kwargs = payload[1] if isinstance(payload[1], dict) else {}
        return args, kwargs

    @classmethod
    def _safe_task_context(cls, task_name: str, args: list, kwargs: dict) -> dict:
        context = {
            "deal_id": None,
            "audit_log_id": None,
            "document": None,
            "force_fresh": None,
        }
        if task_name == "deals.tasks.process_deal_folder_background":
            context["deal_id"] = kwargs.get("deal_id") or (args[0] if args else None)
            context["audit_log_id"] = kwargs.get("audit_log_id")
            context["force_fresh"] = bool(kwargs.get("force_fresh", False))
            files = kwargs.get("file_tree_map") or (args[1] if len(args) > 1 else [])
            if isinstance(files, list):
                context["document_count"] = len(files)
                context["document_names"] = [
                    str(item.get("name") or "Untitled document")
                    for item in files[:50]
                    if isinstance(item, dict)
                ]
        elif task_name == "deals.tasks.process_single_document_async":
            file_info = args[0] if args and isinstance(args[0], dict) else kwargs.get("file_info") or {}
            context["deal_id"] = kwargs.get("deal_id") or (args[1] if len(args) > 1 else None)
            context["audit_log_id"] = kwargs.get("audit_log_id") or (args[4] if len(args) > 4 else None)
            context["force_fresh"] = bool(
                kwargs.get("force_fresh", args[6] if len(args) > 6 else False)
            )
            context["document"] = {
                "source_file_id": file_info.get("id"),
                "name": file_info.get("name") or "Untitled document",
            }
        elif task_name == "deals.tasks.finalize_folder_background":
            context["deal_id"] = kwargs.get("deal_id") or (args[1] if len(args) > 1 else None)
            context["audit_log_id"] = kwargs.get("audit_log_id") or (args[2] if len(args) > 2 else None)
        return context

    @classmethod
    def _decode_message(cls, raw: Any, *, queue: str, position: int) -> dict:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        envelope = json.loads(raw)
        headers = envelope.get("headers") if isinstance(envelope.get("headers"), dict) else {}
        properties = envelope.get("properties") if isinstance(envelope.get("properties"), dict) else {}
        task_name = str(headers.get("task") or "unknown")
        args, kwargs = cls._decode_body(envelope)
        return {
            "queue": queue,
            "position": position,
            "task_id": headers.get("id") or properties.get("correlation_id"),
            "task_name": task_name,
            "display_name": task_name.rsplit(".", 1)[-1].replace("_", " "),
            "eta": headers.get("eta"),
            "retries": headers.get("retries") or 0,
            **cls._safe_task_context(task_name, args, kwargs),
        }

    @classmethod
    def snapshot(cls, celery_app, queues: tuple[str, ...] | None = None) -> dict:
        queue_names = queues or cls.DEFAULT_QUEUES
        result = {"queues": [], "messages": [], "warning": None}
        try:
            with celery_app.connection_for_read() as connection:
                channel = connection.channel()
                client = channel.client
                priority_steps = tuple(getattr(channel, "priority_steps", (0,))) or (0,)
                for queue_name in queue_names:
                    queue_messages = []
                    total_depth = 0
                    for priority in priority_steps:
                        key = channel._q_for_pri(queue_name, priority)
                        depth = int(client.llen(key) or 0)
                        total_depth += depth
                        if depth < 1:
                            continue
                        take = min(depth, cls.MAX_MESSAGES_PER_QUEUE - len(queue_messages))
                        if take < 1:
                            continue
                        start = max(0, depth - take)
                        # Celery pushes on the left and consumes from the right.
                        # Reverse the tail so position 1 is the next ready task.
                        queue_messages.extend(reversed(client.lrange(key, start, depth - 1)))
                    decoded = []
                    for index, raw in enumerate(queue_messages, start=1):
                        try:
                            decoded.append(cls._decode_message(raw, queue=queue_name, position=index))
                        except Exception as exc:
                            decoded.append({
                                "queue": queue_name,
                                "position": index,
                                "task_id": None,
                                "task_name": "unreadable",
                                "display_name": "Unreadable broker message",
                                "decode_error": str(exc)[:300],
                            })
                    result["queues"].append({
                        "name": queue_name,
                        "ready_count": total_depth,
                        "shown_count": len(decoded),
                        "truncated": total_depth > len(decoded),
                    })
                    result["messages"].extend(decoded)
        except Exception as exc:
            result["warning"] = str(exc)[:500]
        return result
