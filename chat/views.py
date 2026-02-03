import logging

import httpx

from .forms import CustomUserAuthenticationForm
from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.conf import settings


logger = logging.getLogger(__name__)


def _ensure_litellm_virtual_key(user):
    if not getattr(settings, "LITELLM_ENABLE_VIRTUAL_KEYS", False):
        return None

    if getattr(user, "litellm_key", ""):
        return user.litellm_key

    master_key = getattr(settings, "LITELLM_MASTER_KEY", None)
    if not master_key:
        raise RuntimeError("LITELLM_MASTER_KEY not set")

    base_url = getattr(settings, "LITELLM_PROXY_BASE_URL", None)
    if not base_url:
        base_url = "http://localhost:8000/litellm"
    base_url = base_url.rstrip("/")

    payload = {
        "key_alias": getattr(user, "email", "user"),
        "models": [],
        "metadata": {"django_user_id": str(user.id), "email": getattr(user, "email", "")},
    }

    auth_headers = {"Authorization": f"Bearer {master_key}", "Content-Type": "application/json"}

    with httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
        resp = client.post(f"{base_url}/key/generate", headers=auth_headers, json=payload)

        # If alias already exists, delete the old key and retry
        if resp.status_code == 400 and "already exists" in resp.text:
            client.post(
                f"{base_url}/key/delete",
                headers=auth_headers,
                json={"key_aliases": [payload["key_alias"]]},
            )
            resp = client.post(f"{base_url}/key/generate", headers=auth_headers, json=payload)

    if resp.status_code >= 400:
        raise RuntimeError(f"LiteLLM key generation failed: {resp.status_code} {resp.text[:512]}")

    data = resp.json()
    key = data.get("key") or data.get("token")
    key_id = data.get("key_id") or data.get("token_id") or data.get("id")
    if not key:
        raise RuntimeError("LiteLLM did not return a key")

    user.litellm_key = key
    if key_id:
        user.litellm_key_id = str(key_id)
    user.save(update_fields=["litellm_key", "litellm_key_id"])
    return key


class CustomLoginView(LoginView):
    authentication_form = CustomUserAuthenticationForm


@login_required
def developer_settings(request):
    """Display API credentials for the authenticated user."""
    error_message = None
    try:
        litellm_key = _ensure_litellm_virtual_key(request.user)
    except Exception as exc:
        litellm_key = ""
        error_message = str(exc)

    litellm_api_base = request.build_absolute_uri("/v1")
    if not request.is_secure():
        litellm_api_base = litellm_api_base.replace("http://", "https://")

    return render(
        request,
        "settings/index.html",
        {
            "litellm_api_base": litellm_api_base,
            "litellm_api_key": litellm_key,
            "error_message": error_message,
        },
    )
