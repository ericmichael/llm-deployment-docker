import requests
import json
import logging
import time
import uuid
from .ai.agent import Agent  # Import the Agent class from the current app directory
from .models import Thread
from .forms import MessageForm, ThreadForm
from .forms import CustomUserAuthenticationForm
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib.auth import get_user_model
from django.contrib.auth.views import LoginView
from django.conf import settings
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.http import StreamingHttpResponse
from rest_framework.authtoken.models import Token
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
)
from rest_framework.authentication import BaseAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.exceptions import AuthenticationFailed


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


def _litellm_headers(request_headers, is_streaming=False):
    headers = {
        "Content-Type": request_headers.get("CONTENT_TYPE", "application/json"),
    }
    service_key = getattr(settings, "LITELLM_SERVICE_KEY", None)
    if service_key:
        headers["Authorization"] = f"Bearer {service_key}"
    if is_streaming:
        headers["Accept"] = "text/event-stream"
    return headers


class BearerAuthentication(BaseAuthentication):
    def authenticate(self, request):
        header = request.META.get("HTTP_AUTHORIZATION")
        if not header:
            return None

        try:
            token = header.split(" ")[1]
        except IndexError:
            raise AuthenticationFailed("Bearer token not provided")

        try:
            user = get_user_model().objects.get(auth_token=token)
        except get_user_model().DoesNotExist:
            raise AuthenticationFailed("No such user")

        return (user, token)


@api_view(["GET", "POST", "PUT", "DELETE", "PATCH"])
@authentication_classes([BearerAuthentication])
@permission_classes([IsAuthenticated])
def litellm_proxy_catchall(request, path=""):
    """
    Catch-all proxy that forwards all requests to LiteLLM.

    Supports all HTTP methods (GET, POST, PUT, DELETE, PATCH) and handles
    both streaming and non-streaming requests.
    """
    from django.http import HttpResponse

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

    # Check if streaming is requested (only for POST requests with body)
    is_streaming = False
    request_data = None
    if request.method in ["POST", "PUT", "PATCH"]:
        try:
            request_data = request.data
            is_streaming = bool(request_data.get("stream", False))
        except Exception:
            request_data = {}
            is_streaming = False

    # Prepare headers
    headers = _litellm_headers(request.META, is_streaming=is_streaming)

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

    # Make the request based on HTTP method
    method = request.method.lower()
    request_func = getattr(requests, method)

    try:
        if is_streaming:
            # Streaming response
            headers.setdefault("Accept", "text/event-stream")

            def generate():
                start = time.monotonic()
                with request_func(
                    upstream_url,
                    json=request_data,
                    params=request.GET.dict(),
                    headers=headers,
                    stream=True,
                ) as response:
                    # Handle error responses
                    if response.status_code >= 400:
                        try:
                            body = response.json()
                            text = json.dumps(body)
                        except Exception:
                            text = response.text

                        if debug:
                            logger.debug(
                                "[LiteLLMProxy][%s] Upstream ERROR %s in %.3fs | body.preview=%s",
                                request_id,
                                response.status_code,
                                time.monotonic() - start,
                                text[:2048],
                            )
                        yield f"event: error\ndata: {text}\n\n"
                        yield "event: done\ndata: [DONE]\n\n"
                        return

                    if debug:
                        logger.debug(
                            "[LiteLLMProxy][%s] Upstream CONNECTED %s in %.3fs",
                            request_id,
                            response.status_code,
                            time.monotonic() - start,
                        )

                    for i, chunk in enumerate(response.iter_lines()):
                        if chunk is None:
                            continue
                        line = chunk.decode("utf-8")
                        if debug and i == 0:
                            logger.debug(
                                "[LiteLLMProxy][%s] First SSE line: %s",
                                request_id,
                                line[:512],
                            )
                        yield line + "\n"

            resp = StreamingHttpResponse(generate(), content_type="text/event-stream")
            if debug:
                resp["X-Debug-Request-Id"] = request_id
                resp["X-Debug-Passthrough"] = "streaming"
            return resp
        else:
            # Non-streaming response
            start = time.monotonic()
            response = request_func(
                upstream_url,
                json=request_data if method in ["post", "put", "patch"] else None,
                params=request.GET.dict(),
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
                resp = Response(body, status=response.status_code)
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
        return Response(
            {"error": str(e)},
            status=500,
        )


@login_required
def developer_settings(request):
    # Get or create the user's token
    token, created = Token.objects.get_or_create(user=request.user)

    # Get the hostname from the request and concatenate it with /api/v1
    api_base = request.build_absolute_uri("/chat/api/v1")
    api_base = (
        api_base.replace("http://", "https://") if not request.is_secure() else api_base
    )

    # Use the token as the API key
    api_key = token.key

    code_block_install = """
pip install -U openai
pip install -U python-dotenv
    """

    code_block_env = f"""
OPENAI_BASE_URL={api_base}
OPENAI_API_KEY={api_key}
"""

    code_block_api_call = """
import os
import openai
from dotenv import load_dotenv

load_dotenv()  # take environment variables from .env

client = OpenAI(
    base_url=os.environ.get("OPENAI_BASE_URL"),
    api_key=os.environ.get("OPENAI_API_KEY"),
)

prompt = "You are a helpful assistant"
message = "Hi! Help me write a 'hello world' program in Java."

messages = [
    {"role": "system", "content": prompt},
    {"role": "user", "content": message}
]

model = "gpt-3.5-turbo"     # use gpt-3.5-turbo model
temperature = 0     # controls randomness

# Make an API call to the OpenAI ChatCompletion endpoint with the model and messages
completion = client.chat.completions.create(
    model=model,
    messages=messages,
    temperature=temperature
)

ai_reply = completion.choices[0].message.content.strip()
print(ai_reply)
"""

    code_block_git_ignore = """
# ... your previous .gitignore
.env    # add this line
"""
    return render(
        request,
        "settings/index.html",
        {
            "api_base": api_base,
            "api_key": api_key,
            "code_block_install": code_block_install,
            "code_block_env": code_block_env,
            "code_block_api_call": code_block_api_call,
            "code_block_git_ignore": code_block_git_ignore,
        },
    )


@login_required
def thread_list(request):
    return render(request, "chat/empty_state.html")


@login_required
def thread_detail(request, pk):
    # Check if the thread belongs to the user
    thread = get_object_or_404(Thread, pk=pk, user=request.user)
    messages = thread.message_set.all()
    return render(
        request,
        "chat/thread_detail.html",
        {
            "thread": thread,
            "messages": messages,
        },
    )


@login_required
def create_thread(request):
    # Generate a default name for the thread, e.g., "Chat on <current date>"
    default_name = f"Chat on {timezone.now().strftime('%Y-%m-%d %H:%M:%S')}"

    # Create a new thread with the default name
    new_thread = Thread.objects.create(name=default_name, user=request.user)

    # Redirect the user to the new thread's detail page
    return redirect("thread_detail", pk=new_thread.pk)


@login_required
@require_POST
def delete_thread(request, pk):
    thread = get_object_or_404(
        Thread, pk=pk, user=request.user
    )  # Check if the thread belongs to the user
    thread.delete()
    return redirect("thread_list")  # Redirect to the thread list view


@login_required
def new_message(request, pk):
    thread = get_object_or_404(Thread, pk=pk)
    if request.method == "POST":
        form = MessageForm(request.POST)
        thread_form = ThreadForm(
            request.POST, instance=thread
        )  # Pass the current thread instance
        if form.is_valid() and thread_form.is_valid():
            message = form.save(commit=False)
            thread = thread_form.save()  # Save the thread form to update the thread
            agent = Agent(thread=thread, prompt=thread.prompt)
            agent.chat(message.content)
            return redirect("thread_detail", pk=thread.pk)
        else:
            print(form.errors)
            print(thread_form.errors)
    else:
        form = MessageForm()
        thread_form = ThreadForm(instance=thread)  # Pass the current thread instance
    return render(
        request,
        "chat/new_message.html",
        {"form": form, "thread_form": thread_form, "thread": thread},
    )
