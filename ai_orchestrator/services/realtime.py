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
        "skill_name": log.skill.name if log.skill else "General Analysis",
    }


def broadcast_audit_log_update(log, *, event_type: str = "snapshot", done: bool = False) -> None:
    channel_layer = get_channel_layer()
    if not channel_layer or not log:
        return

    async_to_sync(channel_layer.group_send)(
        f"ai_stream_{str(log.id)}",
        {
            "type": "ai_message",
            "event_type": event_type,
            "audit_log_id": str(log.id),
            "status": (log.status or "").lower(),
            "done": done,
            "audit_log": serialize_audit_log(log),
        },
    )
