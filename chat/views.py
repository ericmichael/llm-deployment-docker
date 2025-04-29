import requests
import os
import json
from .ai.agent import Agent  # Import the Agent class from the current app directory
from .models import Thread, Message
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
from django.http import JsonResponse
import json
import logging
from openai import OpenAI

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
        def generate():
            # Forward the request to the appropriate API
            with requests.post(
                endpoint,
                json=request_data,
                headers=headers,
                stream=True
            ) as response:
                for chunk in response.iter_lines():
                    if chunk:
                        decoded_chunk = chunk.decode('utf-8')
                        
                        # Add data: prefix for SSE formatting and newlines
                        if decoded_chunk.strip() and not decoded_chunk.startswith('data:'):
                            yield f"data: {decoded_chunk}\n\n"
                        else:
                            yield f"{decoded_chunk}\n\n"
                
                # Add the final closing event
                yield "data: [DONE]\n\n"
                
        return StreamingHttpResponse(
            generate(),
            content_type="text/event-stream"
        )
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
OPENAI_API_BASE={api_base}
OPENAI_API_KEY={api_key}
"""

    code_block_api_call = """
import os
import openai
from dotenv import load_dotenv

load_dotenv()  # take environment variables from .env

client = OpenAI(
    base_url=os.environ.get("OPENAI_API_BASE"),
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
    # if request.method == 'POST':
    #     thread = Thread.objects.create(
    #         user=request.user,
    #         name='New Thread'  # This will be our default_name
    #     )
    #     return JsonResponse({'threadId': thread.id})
    # return JsonResponse({'error': 'Invalid request method'}, status=400)


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
        thread_form = ThreadForm(request.POST, instance=thread)
        if form.is_valid() and thread_form.is_valid():
            message = form.save(commit=False)
            
            # Check if this is the first message in the thread
            if thread.message_set.count() == 0:
                try:
                    # Configure OpenAI client with proper settings
                    if settings.OPENAI_API_TYPE == "azure":
                        client = OpenAI(
                            api_key=settings.AZURE_OPENAI_API_KEY,
                            base_url=f"{settings.AZURE_OPENAI_ENDPOINT}/openai/deployments/{settings.AZURE_OPENAI_DEPLOYMENT_NAME}/",
                            api_version=settings.OPENAI_API_VERSION
                        )
                    else:
                        client = OpenAI(api_key=settings.OPENAI_API_KEY)

                    response = client.chat.completions.create(
                        model="gpt-3.5-turbo",
                        messages=[
                            {"role": "system", "content": "Generate a brief 27-28 character description of the following conversation starter. Make it concise but meaningful."},
                            {"role": "user", "content": form.cleaned_data['content']}
                        ],
                        max_tokens=50,
                        temperature=0.7
                    )
                    new_thread_name = response.choices[0].message.content.strip()
                    thread.name = new_thread_name
                    thread.save()
                except Exception as e:
                    logger.error(f"Error generating thread name: {e}")
                    # Keep default name if generation fails
                    pass

            thread = thread_form.save()
            agent = Agent(thread=thread, prompt=thread.prompt)
            agent.chat(message.content)
            return redirect("thread_detail", pk=thread.pk)
        else:
            print(form.errors)
            print(thread_form.errors)
    else:
        form = MessageForm()
        thread_form = ThreadForm(instance=thread)
    return render(
        request,
        "chat/new_message.html",
        {"form": form, "thread_form": thread_form, "thread": thread},
    )


# def create_message(request):
#     if request.method == 'POST':
#         data = json.loads(request.body)
#         thread_id = data.get('threadId')
#         content = data.get('content')
        
#         thread = get_object_or_404(Thread, id=thread_id, user=request.user)
        
#         # Check if this is the first message in the thread
#         if thread.message_set.count() == 0:
#             # Generate thread name based on the first prompt
#             try:
#                 response = client.chat.completions.create(
#                     model="gpt-3.5-turbo",
#                     messages=[
#                         {"role": "system", "content": "Generate a brief 27-28 character description of the following conversation starter. Make it concise but meaningful."},
#                         {"role": "user", "content": content}
#                     ],
#                     max_tokens=50,
#                     temperature=0.7
#                 )
#                 new_thread_name = response.choices[0].message.content.strip()
#                 # Update thread name
#                 thread.name = new_thread_name
#                 thread.save()
#             except Exception as e:
#                 logger.error(f"Error generating thread name: {e}")
#                 # Keep default name if generation fails
#                 pass

#         # Create the message
#         message = Message.objects.create(
#             thread=thread,
#             role='user',
#             content=content
#         )
        
