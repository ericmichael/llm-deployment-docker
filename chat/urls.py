from django.urls import path, re_path
from . import views

urlpatterns = [
    path("", views.thread_list, name="thread_list"),  # Add this line if needed
    path(
        "thread/<int:pk>/", views.thread_detail, name="thread_detail"
    ),  # GET request to retrieve a specific thread.
    path(
        "thread/", views.create_thread, name="create_thread"
    ),  # POST request to create a new thread.
    path(
        "thread/<int:pk>/messages/", views.new_message, name="new_message"
    ),  # POST request to create a new message in a thread.
    path(
        "thread/<int:pk>/delete", views.delete_thread, name="delete_thread"
    ),  # DELETE request to delete a specific thread.
    path(
        "api/v1/chat/completions",
        views.openai_api_chat_completions_passthrough,
        name="openai_api_chat_completions_passthrough",
    ),
    path(
        "api/v1/completions",
        views.openai_api_completions_passthrough,
        name="openai_api_completions_passthrough",
    ),
    path(
        "api/v1/responses",
        views.openai_api_responses_passthrough,
        name="openai_api_responses_passthrough",
    ),
    path("settings/", views.developer_settings, name="settings"),
]
