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
    # Catch-all proxy for LiteLLM - forwards all /chat/api/v1/* to http://localhost:4000/v1/*
    re_path(
        r"^api/v1/(?P<path>.*)$",
        views.litellm_proxy_catchall,
        name="litellm_proxy_catchall",
    ),
    path("settings/", views.developer_settings, name="settings"),
]
