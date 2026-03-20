from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r'ws/ai-stream/(?P<audit_log_id>[^/]+)/$', consumers.AIStreamConsumer.as_asgi()),
]
