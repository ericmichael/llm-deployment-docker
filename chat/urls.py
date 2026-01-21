from django.urls import path, re_path
from . import views

urlpatterns = [
    # Catch-all proxy for LiteLLM - forwards all /chat/api/v1/* to http://localhost:4000/v1/*
    re_path(
        r"^api/v1/(?P<path>.*)$",
        views.litellm_proxy_catchall,
        name="litellm_proxy_catchall",
    ),
    path("settings/", views.developer_settings, name="settings"),
]
