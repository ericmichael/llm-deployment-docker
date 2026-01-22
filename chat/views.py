import httpx
import json
import logging
import time
import uuid

# Request timeout configuration (seconds)
REQUEST_TIMEOUT = 120  # 2 minutes for LLM requests
CONNECT_TIMEOUT = 10   # 10 seconds to establish connection

from .forms import CustomUserAuthenticationForm
from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.contrib.auth import get_user_model
from django.contrib.auth.views import LoginView
from django.conf import settings
from django.http import StreamingHttpResponse, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from asgiref.sync import sync_to_async
from rest_framework.authtoken.models import Token


logger = logging.getLogger(__name__)


class CustomLoginView(LoginView):
    authentication_form = CustomUserAuthenticationForm


def _litellm_base_url():
    base = getattr(settings, "LITELLM_BASE_URL", "https://api.openai.com/v1")
    if not base:
        return "https://api.openai.com/v1"
    return base.rstrip("/")


def _litellm_url(path: str) -> str:
    return f"{_litellm_base_url()}{path}"


def _litellm_headers(is_streaming=False):
    headers = {
        "Content-Type": "application/json",
    }
    service_key = getattr(settings, "LITELLM_SERVICE_KEY", None)
    if service_key:
        headers["Authorization"] = f"Bearer {service_key}"
        headers["api-key"] = service_key

    if is_streaming:
        headers["Accept"] = "text/event-stream"
    return headers


async def _authenticate_bearer(request):
    """
    Authenticate request using Bearer token.

    Returns:
        tuple: (user, token_key) if authenticated and authorized
        None: if token is invalid
        "inactive": if user has no active course enrollment
    """
    header = request.META.get("HTTP_AUTHORIZATION", "")
    if not header.startswith("Bearer "):
        return None

    token_key = header[7:]  # Strip "Bearer "
    if not token_key:
        return None

    try:
        token = await sync_to_async(Token.objects.select_related("user").get)(key=token_key)
        user = token.user

        # Superusers bypass enrollment check
        if user.is_superuser:
            return (user, token_key)

        # Check for active course enrollment
        has_active = await sync_to_async(user.has_active_enrollment)()
        if not has_active:
            return "inactive"

        return (user, token_key)
    except Token.DoesNotExist:
        return None


@csrf_exempt
async def litellm_proxy_catchall(request, path=""):
    """
    Catch-all proxy that forwards all requests to LiteLLM.

    Supports all HTTP methods (GET, POST, PUT, DELETE, PATCH) and handles
    both streaming and non-streaming requests.
    """
    # Authenticate
    auth_result = await _authenticate_bearer(request)
    if auth_result is None:
        return JsonResponse({"error": "Authentication required"}, status=401)
    if auth_result == "inactive":
        return JsonResponse(
            {"error": "Access denied. You are not enrolled in an active course."},
            status=403,
        )

    # Prepend /v1/ if the path doesn't already start with it
    if not path.startswith("v1/"):
        path = f"v1/{path}"

    # Build the upstream URL
    upstream_url = _litellm_url(f"/{path}")

    # Debug toggles
    debug_enabled = getattr(settings, "DEBUG_OPENAI_PASSTHROUGH", False)
    debug_param = request.GET.get("debug", "").lower() in ("1", "true", "yes", "on")
    debug = bool(debug_enabled or debug_param)
    request_id = request.META.get("HTTP_X_REQUEST_ID", str(uuid.uuid4()))

    def _sanitize_headers(h: dict) -> dict:
        redacted = {}
        for k, v in h.items():
            key = str(k).lower()
            if key in ("authorization", "api-key", "x-api-key"):
                redacted[k] = "[REDACTED]"
            else:
                redacted[k] = v
        return redacted

    # Parse request body for POST/PUT/PATCH
    is_streaming = False
    request_data = None
    if request.method in ["POST", "PUT", "PATCH"]:
        try:
            body = request.body
            if body:
                request_data = json.loads(body)
                is_streaming = bool(request_data.get("stream", False))
        except (json.JSONDecodeError, Exception):
            request_data = {}
            is_streaming = False

    # Prepare headers
    headers = _litellm_headers(is_streaming=is_streaming)

    # Log outgoing request
    if debug:
        try:
            logger.debug(
                "[LiteLLMProxy][%s] %s -> %s | stream=%s | headers=%s | payload.preview=%s",
                request_id,
                request.method,
                upstream_url,
                is_streaming,
                _sanitize_headers(headers),
                json.dumps(request_data)[:2048] if request_data else "N/A",
            )
        except Exception:
            logger.debug(
                "[LiteLLMProxy][%s] %s -> %s (payload preview unavailable)",
                request_id,
                request.method,
                upstream_url,
            )

    method = request.method.lower()
    timeout = httpx.Timeout(REQUEST_TIMEOUT, connect=CONNECT_TIMEOUT)
    query_params = dict(request.GET)

    try:
        if is_streaming:
            # Streaming response using async httpx
            headers["Accept"] = "text/event-stream"

            async def generate():
                """Async generator for streaming responses."""
                start = time.monotonic()

                def serialize_error(message):
                    return json.dumps({"error": message})

                done_event = "event: done\ndata: [DONE]\n\n"

                try:
                    async with httpx.AsyncClient(timeout=timeout) as client:
                        async with client.stream(
                            method,
                            upstream_url,
                            json=request_data,
                            params=query_params,
                            headers=headers,
                        ) as response:
                            if response.status_code >= 400:
                                body = await response.aread()
                                try:
                                    text = body.decode("utf-8")
                                except Exception:
                                    text = str(body)

                                if debug:
                                    logger.debug(
                                        "[LiteLLMProxy][%s] Upstream ERROR %s in %.3fs | body.preview=%s",
                                        request_id,
                                        response.status_code,
                                        time.monotonic() - start,
                                        text[:2048],
                                    )
                                yield f"event: error\ndata: {text}\n\n"
                                yield done_event
                                return

                            if debug:
                                logger.debug(
                                    "[LiteLLMProxy][%s] Upstream CONNECTED %s in %.3fs",
                                    request_id,
                                    response.status_code,
                                    time.monotonic() - start,
                                )

                            i = 0
                            async for line in response.aiter_lines():
                                if debug and i == 0 and line:
                                    logger.debug(
                                        "[LiteLLMProxy][%s] First SSE line: %s",
                                        request_id,
                                        line[:512],
                                    )
                                yield line + "\n"
                                i += 1

                except httpx.RequestError as exc:
                    message = str(exc)
                    logger.error(
                        "[LiteLLMProxy][%s] Streaming failure after %.3fs: %s",
                        request_id,
                        time.monotonic() - start,
                        message,
                    )
                    payload = serialize_error(message)
                    yield f"event: error\ndata: {payload}\n\n"
                    yield done_event
                except Exception as exc:
                    message = str(exc)
                    logger.error(
                        "[LiteLLMProxy][%s] Unexpected streaming error after %.3fs: %s",
                        request_id,
                        time.monotonic() - start,
                        message,
                        exc_info=True,
                    )
                    payload = serialize_error(message)
                    yield f"event: error\ndata: {payload}\n\n"
                    yield done_event

            resp = StreamingHttpResponse(generate(), content_type="text/event-stream")
            if debug:
                resp["X-Debug-Request-Id"] = request_id
                resp["X-Debug-Passthrough"] = "streaming"
            return resp
        else:
            # Non-streaming response
            start = time.monotonic()
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.request(
                    method,
                    upstream_url,
                    json=request_data if method in ["post", "put", "patch"] else None,
                    params=query_params,
                    headers=headers,
                )
            elapsed = time.monotonic() - start

            # Attempt JSON; fallback to text
            try:
                body = response.json()
                is_json = True
            except Exception:
                body = response.text
                is_json = False

            if debug:
                logger.debug(
                    "[LiteLLMProxy][%s] Upstream %s in %.3fs | ct=%s | body.preview=%s",
                    request_id,
                    response.status_code,
                    elapsed,
                    response.headers.get("Content-Type"),
                    (json.dumps(body) if is_json else str(body))[:2048],
                )

            if is_json:
                resp = JsonResponse(body, status=response.status_code, safe=False)
            else:
                resp = HttpResponse(
                    body,
                    status=response.status_code,
                    content_type=response.headers.get("Content-Type", "text/plain"),
                )

            if debug:
                resp["X-Debug-Request-Id"] = request_id
                resp["X-Debug-Passthrough"] = "non-streaming"
                resp["X-Upstream-Status"] = str(response.status_code)
            return resp

    except Exception as e:
        logger.error(
            "[LiteLLMProxy][%s] Exception: %s",
            request_id,
            str(e),
            exc_info=True,
        )
        return JsonResponse({"error": str(e)}, status=500)


@login_required
def developer_settings(request):
    """Display API credentials for the authenticated user."""
    # Get or create the user's token
    token, created = Token.objects.get_or_create(user=request.user)

    # Build the API base URL, ensuring HTTPS
    api_base = request.build_absolute_uri("/chat/api/v1")
    if not request.is_secure():
        api_base = api_base.replace("http://", "https://")

    return render(
        request,
        "settings/index.html",
        {
            "api_base": api_base,
            "api_key": token.key,
        },
    )
