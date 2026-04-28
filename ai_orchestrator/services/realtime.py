from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer


def serialize_audit_log(log) -> dict:
    return {
        "id": str(log.id),
        "source_type": log.source_type,
        "source_id": log.source_id,
        "context_label": log.context_label,
        "model_used": log.model_used,
        "status": log.status,
        "is_success": log.is_success,
        "created_at": log.created_at.isoformat() if log.created_at else None,
        "request_duration_ms": log.request_duration_ms,
        "tokens_used": log.tokens_used,
        "error_message": log.error_message,
        "raw_response": log.raw_response,
        "raw_thinking": log.raw_thinking,
        "user_prompt": log.user_prompt,
        "system_prompt": log.system_prompt,
        "parsed_json": log.parsed_json,
        "source_metadata": log.source_metadata,
        "worker_logs": log.worker_logs or [],
        "skill_name": log.skill.name if log.skill else "General Analysis",
        "personality_name": log.personality.name if log.personality else "Direct Inference",
        "celery_task_id": log.celery_task_id,
    }


def broadcast_audit_log_update(log, *, event_type: str = "snapshot", done: bool = False) -> None:
    channel_layer = get_channel_layer()
    if not channel_layer or not log:
        return

    # 1. Full payload for the specific log stream (e.g., detail view)
    detail_data = {
        "type": "ai_message",
        "event_type": event_type,
        "audit_log_id": str(log.id),
        "status": (log.status or "").lower(),
        "done": done,
        "audit_log": serialize_audit_log(log),
    }

    async_to_sync(channel_layer.group_send)(
        f"ai_stream_{str(log.id)}",
        detail_data,
    )

    # 2. Lightweight payload for the general ledger (reduces "over capacity" errors)
    ledger_data = {
        "type": "ai_message",
        "event_type": "ledger_update",
        "audit_log_id": str(log.id),
        "status": (log.status or "").lower(),
        "done": done,
        "context_label": log.context_label,
        "last_log_entry": log.worker_logs[-1] if log.worker_logs else None,
    }

    async_to_sync(channel_layer.group_send)(
        "audit_logs_general",
        ledger_data,
    )


def log_worker_event(log, message: str, *, status: str = None, event_type: str = "snapshot", done: bool = False) -> None:
    """
    Appends a message to the AIAuditLog.worker_logs list and broadcasts the update.
    """
    if not log:
        return

    # Update the log in DB
    if not log.worker_logs:
        log.worker_logs = []
    
    log.worker_logs.append(message)
    if status:
        log.status = status
    
    log.save(update_fields=['worker_logs', 'status'] if status else ['worker_logs'])

    # Broadcast
    broadcast_audit_log_update(log, event_type=event_type, done=done)
