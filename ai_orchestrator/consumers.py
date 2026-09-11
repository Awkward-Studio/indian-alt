import json
import logging
from urllib.parse import parse_qs

from asgiref.sync import sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.core import signing
from django.core.cache import cache
from django.contrib.auth import get_user_model

logger = logging.getLogger(__name__)


@sync_to_async
def _user_from_ticket(ticket, *, required_scope, audit_log_id=None):
    if not ticket:
        return None
    try:
        signed = signing.loads(ticket, salt="ai-websocket-ticket", max_age=60)
        nonce = signed.get("nonce")
        cache_key = f"ai-ws-ticket:{nonce}"
        payload = cache.get(cache_key)
        if not payload or payload != signed:
            return None
        cache.delete(cache_key)
        if payload.get("scope") != required_scope:
            return None
        if required_scope == "audit" and str(payload.get("audit_log_id")) != str(audit_log_id):
            return None
        user_id = payload.get("user_id")
        if not user_id:
            return None
        user = get_user_model().objects.get(pk=user_id)
        return user if user.is_active else None
    except (signing.BadSignature, signing.SignatureExpired, get_user_model().DoesNotExist):
        return None


def _ticket_from_scope(scope):
    query_params = parse_qs(scope.get("query_string", b"").decode("utf-8"))
    return query_params.get("ticket", [None])[0]

class AIStreamConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for streaming AI responses and thinking blocks.
    Users connect to ws/ai-stream/<audit_log_id>/
    """
    async def connect(self):
        self.audit_log_id = self.scope['url_route']['kwargs']['audit_log_id']
        self.user = await _user_from_ticket(
            _ticket_from_scope(self.scope), required_scope="audit", audit_log_id=self.audit_log_id,
        )
        if not self.user:
            await self.close(code=4401)
            return

        self.room_group_name = f'ai_stream_{self.audit_log_id}'
        self.joined_group = False

        try:
            await self.channel_layer.group_add(
                self.room_group_name,
                self.channel_name
            )
            self.joined_group = True
        except Exception as exc:
            logger.warning("AI stream websocket unavailable for %s: %s", self.audit_log_id, exc)
            await self.close(code=1013)
            return

        await self.accept()
        logger.info(f"WebSocket connected: {self.room_group_name}")

    async def disconnect(self, close_code):
        if getattr(self, "joined_group", False):
            try:
                await self.channel_layer.group_discard(
                    self.room_group_name,
                    self.channel_name
                )
            except Exception as exc:
                logger.warning("AI stream websocket cleanup failed for %s: %s", self.audit_log_id, exc)
        logger.info(f"WebSocket disconnected: {self.room_group_name}")

    # Receive message from room group
    async def ai_message(self, event):
        """
        Handles messages sent to the group via channel_layer.group_send
        """
        # Send message to WebSocket
        await self.send(text_data=json.dumps({
            'event_type': event.get('event_type', 'delta'),
            'audit_log_id': event.get('audit_log_id'),
            'response': event.get('response', ''),
            'thinking': event.get('thinking', ''),
            'response_delta': event.get('response_delta', event.get('response', '')),
            'thinking_delta': event.get('thinking_delta', event.get('thinking', '')),
            'status': event.get('status', 'processing'),
            'done': event.get('done', False),
            'audit_log': event.get('audit_log'),
        }))

class AIAuditLogConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for global audit log updates.
    Users connect to ws/audit-logs/ to see state changes across the entire ledger.
    """
    async def connect(self):
        self.user = await _user_from_ticket(_ticket_from_scope(self.scope), required_scope="ledger")
        if not self.user:
            await self.close(code=4401)
            return

        self.room_group_name = 'audit_logs_general'
        self.joined_group = False

        try:
            await self.channel_layer.group_add(
                self.room_group_name,
                self.channel_name
            )
            self.joined_group = True
        except Exception as exc:
            logger.warning("Global audit ledger websocket unavailable: %s", exc)
            await self.close(code=1013)
            return

        await self.accept()
        logger.info(f"WebSocket connected: {self.room_group_name}")

    async def disconnect(self, close_code):
        if getattr(self, "joined_group", False):
            try:
                await self.channel_layer.group_discard(
                    self.room_group_name,
                    self.channel_name
                )
            except Exception as exc:
                logger.warning("Global audit ledger websocket cleanup failed: %s", exc)
        logger.info(f"WebSocket disconnected: {self.room_group_name}")

    async def ai_message(self, event):
        """
        Handles messages sent to the group via channel_layer.group_send
        """
        # Send message to WebSocket
        await self.send(text_data=json.dumps({
            'event_type': event.get('event_type', 'snapshot'),
            'audit_log_id': event.get('audit_log_id'),
            'status': event.get('status', 'processing'),
            'done': event.get('done', False),
            'audit_log': event.get('audit_log'),
            'context_label': event.get('context_label'),
            'last_log_entry': event.get('last_log_entry'),
            'pipeline_id': event.get('pipeline_id'),
            'pipeline_key': event.get('pipeline_key'),
            'pipeline_stage_id': event.get('pipeline_stage_id'),
            'pipeline_stage_key': event.get('pipeline_stage_key'),
            'started_at': event.get('started_at'),
            'completed_at': event.get('completed_at'),
            'duration_ms': event.get('duration_ms'),
            'error': event.get('error'),
            'active_runs': event.get('active_runs'),
        }))
