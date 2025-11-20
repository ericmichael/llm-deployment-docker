"""
WebSocket URL routing for the chat application.

Defines WebSocket endpoints that students can connect to for realtime API access.
"""
from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r'ws/v1/realtime$', consumers.RealtimeProxyConsumer.as_asgi()),
    re_path(r'ws/realtime$', consumers.RealtimeProxyConsumer.as_asgi()),
]
