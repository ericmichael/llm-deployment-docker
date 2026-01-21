"""
WebSocket consumers for proxying realtime API requests to LiteLLM.

This module provides WebSocket passthrough functionality that allows students
to connect to the realtime API using their Django authentication tokens while
the server handles the LiteLLM service key internally.
"""
import asyncio
import logging
import websockets
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.conf import settings
from rest_framework.authtoken.models import Token
from urllib.parse import parse_qs, urlencode


logger = logging.getLogger(__name__)

# WebSocket configuration
WS_MAX_SIZE = 10 * 1024 * 1024  # 10 MB max message size
WS_CONNECT_TIMEOUT = 30  # 30 seconds to connect to backend
WS_CLOSE_TIMEOUT = 10  # 10 seconds for close handshake
WS_PING_INTERVAL = 20  # Send ping every 20 seconds
WS_PING_TIMEOUT = 20  # Wait 20 seconds for pong response
WS_IDLE_TIMEOUT = 300  # 5 minutes idle timeout


class RealtimeProxyConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer that proxies realtime API requests to LiteLLM.

    Authentication:
    - Accepts Django auth tokens via query param ?token=... or Authorization header
    - Validates the token against the Django user database
    - Forwards authenticated requests to LiteLLM with the service key

    Flow:
    1. Student connects with their auth token
    2. Consumer validates the token
    3. Consumer connects to LiteLLM with service key
    4. Bidirectional message forwarding begins
    """

    async def connect(self):
        """
        Handle WebSocket connection.

        - Extract and validate auth token
        - Connect to LiteLLM backend
        - Start message forwarding
        """
        # Extract token from query params or headers
        query_string = self.scope.get("query_string", b"").decode()
        params = parse_qs(query_string)

        token = None
        headers = dict(self.scope.get("headers", []))
        api_key_header_value = headers.get(b"api-key", b"").decode()
        api_key_query_values = params.get("api-key") or params.get("api_key") or []
        api_key_query_value = api_key_query_values[0] if api_key_query_values else ""
        auth_api_key_value = None
        token = None

        if "token" in params:
            token = params["token"][0]
        else:
            auth_header = headers.get(b"authorization", b"").decode()
            if auth_header.startswith("Bearer "):
                token = auth_header[7:]

            if not token and api_key_header_value:
                token = api_key_header_value
                auth_api_key_value = api_key_header_value
            elif not token and api_key_query_value:
                token = api_key_query_value
                auth_api_key_value = api_key_query_value

        if not token:
            logger.warning("WebSocket connection rejected: No token provided")
            await self.close(code=4001)
            return

        # Authenticate user
        try:
            token_obj = await database_sync_to_async(Token.objects.get)(key=token)
            self.user = await database_sync_to_async(lambda: token_obj.user)()
            logger.info(f"WebSocket connection authenticated for user: {self.user.email}")

            # Check for active course enrollment (superusers bypass this check)
            if not self.user.is_superuser:
                has_active = await database_sync_to_async(self.user.has_active_enrollment)()
                if not has_active:
                    logger.warning(f"WebSocket rejected: User {self.user.email} has no active enrollment")
                    await self.close(code=4003)
                    return
        except Token.DoesNotExist:
            logger.warning("WebSocket connection rejected: Invalid token")
            await self.close(code=4001)
            return

        # Extract model from query params
        self.model = params.get("model", ["gpt-realtime"])[0]

        # Extract optional intent parameter
        self.intent = params.get("intent", [None])[0]

        # Accept the WebSocket connection
        await self.accept()

        # Connect to LiteLLM proxy
        litellm_base = getattr(settings, "LITELLM_BASE_URL", "http://127.0.0.1:4000")
        litellm_ws_url = litellm_base.replace("http://", "ws://").replace("https://", "wss://")

        # Build the upstream URL with query params
        query_params = {"model": self.model}
        if self.intent:
            query_params["intent"] = self.intent

        litellm_url = f"{litellm_ws_url}/v1/realtime?{urlencode(query_params)}"

        service_key = getattr(settings, "LITELLM_SERVICE_KEY", None)
        headers = {}
        if service_key:
            headers["Authorization"] = f"Bearer {service_key}"
            headers["api-key"] = service_key

        try:
            logger.info(f"Connecting to LiteLLM: {litellm_url}")
            self.backend_ws = await asyncio.wait_for(
                websockets.connect(
                    litellm_url,
                    additional_headers=headers,
                    max_size=WS_MAX_SIZE,
                    close_timeout=WS_CLOSE_TIMEOUT,
                    ping_interval=WS_PING_INTERVAL,
                    ping_timeout=WS_PING_TIMEOUT,
                ),
                timeout=WS_CONNECT_TIMEOUT,
            )

            # Track last activity for idle timeout
            self.last_activity = asyncio.get_event_loop().time()

            # Start bidirectional forwarding and idle timeout checker
            self.forward_task = asyncio.create_task(self.forward_from_backend())
            self.idle_task = asyncio.create_task(self.check_idle_timeout())
            logger.info(f"WebSocket proxy established for user {self.user.email}")

        except asyncio.TimeoutError:
            logger.error(f"Timeout connecting to LiteLLM after {WS_CONNECT_TIMEOUT}s")
            await self.close(code=1011)
        except Exception as e:
            logger.error(f"Failed to connect to LiteLLM: {e}", exc_info=True)
            await self.close(code=1011)

    async def disconnect(self, close_code):
        """
        Handle WebSocket disconnection.

        - Cancel forwarding task
        - Cancel idle timeout task
        - Close backend connection
        """
        logger.info(f"WebSocket disconnecting for user {getattr(self, 'user', 'unknown')}: code={close_code}")

        # Cancel forwarding task
        if hasattr(self, 'forward_task'):
            self.forward_task.cancel()
            try:
                await self.forward_task
            except asyncio.CancelledError:
                pass

        # Cancel idle timeout task
        if hasattr(self, 'idle_task'):
            self.idle_task.cancel()
            try:
                await self.idle_task
            except asyncio.CancelledError:
                pass

        # Close backend connection
        if hasattr(self, 'backend_ws'):
            await self.backend_ws.close()

    async def receive(self, text_data=None, bytes_data=None):
        """
        Forward client messages to LiteLLM backend.

        Args:
            text_data: Text message from client
            bytes_data: Binary message from client
        """
        # Update last activity timestamp
        self.last_activity = asyncio.get_event_loop().time()

        if hasattr(self, 'backend_ws'):
            try:
                if text_data:
                    await self.backend_ws.send(text_data)
                elif bytes_data:
                    await self.backend_ws.send(bytes_data)
            except Exception as e:
                logger.error(f"Error forwarding to backend: {e}", exc_info=True)
                await self.close(code=1011)

    async def forward_from_backend(self):
        """
        Forward messages from LiteLLM backend to client.

        Runs as a background task that continuously forwards messages
        from the LiteLLM backend to the connected client.
        """
        try:
            async for message in self.backend_ws:
                # Update last activity timestamp
                self.last_activity = asyncio.get_event_loop().time()

                if isinstance(message, bytes):
                    await self.send(bytes_data=message)
                else:
                    await self.send(text_data=message)
        except websockets.exceptions.ConnectionClosed:
            logger.info(f"Backend connection closed for user {self.user.email}")
            await self.close(code=1000)
        except Exception as e:
            logger.error(f"Error in backend forwarding: {e}", exc_info=True)
            await self.close(code=1011)

    async def check_idle_timeout(self):
        """
        Check for idle connections and close them after WS_IDLE_TIMEOUT seconds.

        Runs as a background task that periodically checks the last activity
        timestamp and closes the connection if it's been idle too long.
        """
        try:
            while True:
                await asyncio.sleep(30)  # Check every 30 seconds
                current_time = asyncio.get_event_loop().time()
                idle_duration = current_time - self.last_activity

                if idle_duration > WS_IDLE_TIMEOUT:
                    logger.info(
                        f"Closing idle WebSocket for user {self.user.email} "
                        f"(idle for {idle_duration:.0f}s)"
                    )
                    await self.close(code=1000)
                    break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error in idle timeout checker: {e}", exc_info=True)
