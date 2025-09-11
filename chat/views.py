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


@api_view(["POST"])
@authentication_classes([BearerAuthentication])
@permission_classes([IsAuthenticated])
def openai_api_responses_passthrough(request):
    """
    Passthrough to the OpenAI/Azure OpenAI Responses API.

    - Azure endpoint: {AZURE_OPENAI_ENDPOINT}/openai/responses?api-version={AZURE_OPENAI_INFERENCE_API_VERSION}
    - OpenAI endpoint: https://api.openai.com/v1/responses

    Supports streaming when the request body sets `stream: true`.
    """
    request_data = request.data
    request_headers = request.META

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

    # Check if streaming is requested
    is_streaming = False
    try:
        # request.data may be QueryDict or dict-like; be defensive
        is_streaming = bool(request_data.get("stream", False))
    except Exception:
        is_streaming = False

    # Determine the API key and endpoint based on configuration
    if settings.OPENAI_API_TYPE == "azure":
        api_key = settings.AZURE_OPENAI_API_KEY
        api_version = settings.AZURE_OPENAI_INFERENCE_API_VERSION
        endpoint = f"{settings.AZURE_OPENAI_ENDPOINT}/openai/responses?api-version={api_version}"
        headers = {
            "Content-Type": request_headers.get("CONTENT_TYPE", "application/json"),
            "api-key": api_key,
        }
        # Ensure correct Accept header for streaming
        if is_streaming:
            headers["Accept"] = "text/event-stream"
    else:
        api_key = settings.OPENAI_API_KEY
        endpoint = "https://api.openai.com/v1/responses"
        headers = {
            "Content-Type": request_headers.get("CONTENT_TYPE", "application/json"),
            "Authorization": f"Bearer {api_key}",
        }
        if is_streaming:
            headers["Accept"] = "text/event-stream"

    # Log outgoing request metadata
    if debug:
        try:
            logger.debug(
                "[ResponsesPassthrough][%s] Outgoing POST -> %s | stream=%s | headers=%s | payload.preview=%s",
                request_id,
                endpoint,
                is_streaming,
                _sanitize_headers(headers),
                json.dumps(request_data)[:2048]
                if not isinstance(request_data, (str, bytes))
                else str(request_data)[:2048],
            )
        except Exception:
            logger.debug(
                "[ResponsesPassthrough][%s] (payload preview unavailable)", request_id
            )

    if is_streaming:
        # Ensure upstream knows we want SSE
        headers.setdefault("Accept", "text/event-stream")

        def generate():
            start = time.monotonic()
            with requests.post(
                endpoint,
                json=request_data,
                headers=headers,
                stream=True,
            ) as response:
                # If upstream responded with an error status, don't stream; return the error body instead
                if response.status_code >= 400:
                    try:
                        body = response.json()
                        text = json.dumps(body)
                    except Exception:
                        text = response.text

                    if debug:
                        logger.debug(
                            "[ResponsesPassthrough][%s] Upstream ERROR %s in %.3fs | headers=%s | body.preview=%s",
                            request_id,
                            response.status_code,
                            time.monotonic() - start,
                            _sanitize_headers(response.headers),
                            text[:2048],
                        )
                    # Yield one error event as SSE so client still sees something useful
                    yield f"event: error\ndata: {text}\n\n"
                    yield "event: done\ndata: [DONE]\n\n"
                    return

                if debug:
                    logger.debug(
                        "[ResponsesPassthrough][%s] Upstream CONNECTED %s in %.3fs | headers=%s",
                        request_id,
                        response.status_code,
                        time.monotonic() - start,
                        _sanitize_headers(response.headers),
                    )
                for i, chunk in enumerate(response.iter_lines()):
                    if chunk is None:
                        continue
                    line = chunk.decode("utf-8")
                    if debug and i == 0:
                        logger.debug(
                            "[ResponsesPassthrough][%s] First SSE line: %s",
                            request_id,
                            line[:512],
                        )
                    # Pass through upstream SSE lines unchanged
                    yield line + "\n"

        resp = StreamingHttpResponse(generate(), content_type="text/event-stream")
        if debug:
            resp["X-Debug-Request-Id"] = request_id
            resp["X-Debug-Passthrough"] = "responses-sse"
        return resp
    else:
        start = time.monotonic()
        response = requests.post(
            endpoint,
            json=request_data,
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
                "[ResponsesPassthrough][%s] Upstream %s in %.3fs | ct=%s | body.preview=%s",
                request_id,
                response.status_code,
                elapsed,
                response.headers.get("Content-Type"),
                (json.dumps(body) if is_json else str(body))[:2048],
            )

        if is_json:
            resp = Response(body, status=response.status_code)
        else:
            # Return raw text with upstream content type
            from django.http import HttpResponse

            resp = HttpResponse(
                body,
                status=response.status_code,
                content_type=response.headers.get("Content-Type", "text/plain"),
            )

        if debug:
            resp["X-Debug-Request-Id"] = request_id
            resp["X-Debug-Passthrough"] = "responses-json"
            resp["X-Upstream-Status"] = str(response.status_code)
        return resp


@api_view(["POST"])
@authentication_classes([BearerAuthentication])
@permission_classes([IsAuthenticated])
def openai_api_chat_completions_passthrough(request):
    # Get the request data and headers
    request_data = request.data
    request_headers = request.META

    # Extract the deployment name and API version from the request data
    deployment_name = request_data.get(
        "model", None
    )  # Provide a default if not specified
    api_version = settings.OPENAI_API_VERSION

    # Check if streaming is requested
    is_streaming = request_data.get("stream", False)

    # Determine the API key and endpoint based on configuration
    if settings.OPENAI_API_TYPE == "azure":
        api_key = settings.AZURE_OPENAI_API_KEY
        endpoint = f"{settings.AZURE_OPENAI_ENDPOINT}/openai/deployments/{deployment_name}/chat/completions?api-version={api_version}"
        headers = {
            "Content-Type": request_headers.get("CONTENT_TYPE"),
            "api-key": api_key,
        }
    else:
        api_key = settings.OPENAI_API_KEY
        endpoint = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Content-Type": request_headers.get("CONTENT_TYPE"),
            "Authorization": f"Bearer {api_key}",
        }

    if is_streaming:
        # Stream the response
        # Ensure upstream knows we want SSE
        headers.setdefault("Accept", "text/event-stream")

        def generate():
            # Forward the request to the appropriate API
            with requests.post(
                endpoint,
                json=request_data,
                headers=headers,
                stream=True,
            ) as response:
                for chunk in response.iter_lines():
                    if chunk:
                        decoded_chunk = chunk.decode("utf-8")

                        # Add data: prefix for SSE formatting and newlines
                        if decoded_chunk.strip() and not decoded_chunk.startswith(
                            "data:"
                        ):
                            yield f"data: {decoded_chunk}\n\n"
                        else:
                            yield f"{decoded_chunk}\n\n"

                # Add the final closing event
                yield "data: [DONE]\n\n"

        return StreamingHttpResponse(generate(), content_type="text/event-stream")
    else:
        # Non-streaming behavior - forward the request and return complete response
        response = requests.post(
            endpoint,
            json=request_data,
            headers=headers,
        )

        # Return the API response
        return Response(response.json())


@api_view(["POST"])
@authentication_classes([BearerAuthentication])
@permission_classes([IsAuthenticated])
def openai_api_completions_passthrough(request):
    # Get the request data and headers
    request_data = request.data
    request_headers = request.META

    # Extract the deployment name and API version from the request data
    deployment_name = request_data.get("model")
    api_version = settings.OPENAI_API_VERSION

    # Determine the API key and endpoint based on configuration
    if settings.OPENAI_API_TYPE == "azure":
        api_key = settings.AZURE_OPENAI_API_KEY

        endpoint = f"{settings.AZURE_OPENAI_ENDPOINT}/openai/deployments/{deployment_name}/completions?api-version={api_version}"
        headers = {
            "Content-Type": request_headers.get("CONTENT_TYPE"),
            "api-key": api_key,
        }
    else:
        api_key = settings.OPENAI_API_KEY
        endpoint = "https://api.openai.com/v1/completions"
        headers = {
            "Content-Type": request_headers.get("CONTENT_TYPE"),
            "Authorization": f"Bearer {api_key}",
        }

    # Forward the request to the appropriate API
    response = requests.post(
        endpoint,
        json=request_data,
        headers=headers,
    )

    # Return the API response
    return Response(response.json())


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
