import json
from channels.generic.websocket import AsyncWebsocketConsumer
import logging

logger = logging.getLogger(__name__)

class AIStreamConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for streaming AI responses and thinking blocks.
    Users connect to ws/ai-stream/<audit_log_id>/
    """
    async def connect(self):
        self.audit_log_id = self.scope['url_route']['kwargs']['audit_log_id']
        self.room_group_name = f'ai_stream_{self.audit_log_id}'

        # Join room group
        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )

        await self.accept()
        logger.info(f"WebSocket connected: {self.room_group_name}")

    async def disconnect(self, close_code):
        # Leave room group
        await self.channel_layer.group_discard(
            self.room_group_name,
            self.channel_name
        )
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
