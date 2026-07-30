import logging

from . import litellm_keys
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

    key, key_id = litellm_keys.generate_key(user)

    user.litellm_key = key
    if key_id:
        user.litellm_key_id = key_id
    user.save(update_fields=["litellm_key", "litellm_key_id"])
    return key


class CustomLoginView(LoginView):
    authentication_form = CustomUserAuthenticationForm


@login_required
def developer_settings(request):
    """Display API credentials for the authenticated user."""
    error_message = None
    litellm_key = ""

    # Staff always get keys; other users need an active enrollment
    if request.user.is_staff or request.user.has_active_enrollment():
        try:
            litellm_key = _ensure_litellm_virtual_key(request.user)
        except Exception as exc:
            error_message = str(exc)
    else:
        error_message = (
            "You are not enrolled in any active course. "
            "Contact your instructor to be added."
        )

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
