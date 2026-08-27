import logging

from . import litellm_keys
from .forms import CustomUserAuthenticationForm
from django.contrib import messages
from django.shortcuts import redirect, render
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.contrib.auth.views import LoginView
from django.conf import settings


logger = logging.getLogger(__name__)


def _ensure_litellm_virtual_key(user):
    if not getattr(settings, "LITELLM_ENABLE_VIRTUAL_KEYS", False):
        return ""
    return litellm_keys.ensure_key(user)


class CustomLoginView(LoginView):
    authentication_form = CustomUserAuthenticationForm


@login_required
def developer_settings(request):
    """Display API credentials for the authenticated user."""
    error_message = None
    litellm_key = ""
    usage = None
    budget, budget_source = litellm_keys.effective_budget_with_source(request.user)

    # Staff always get keys; other users need an active enrollment
    if request.user.is_staff or request.user.has_active_enrollment():
        try:
            litellm_key = _ensure_litellm_virtual_key(request.user)
            if litellm_key:
                usage = litellm_keys.key_usage(request.user)
        except Exception:
            logger.exception("Could not provision API key for %s", request.user.email)
            error_message = "Could not provision your API key right now. Please try again later."
    else:
        error_message = (
            "You are not enrolled in any active course. "
            "Contact your instructor to be added."
        )

    # Scheme is correct behind Azure thanks to SECURE_PROXY_SSL_HEADER.
    litellm_api_base = request.build_absolute_uri("/v1")

    return render(
        request,
        "settings/index.html",
        {
            "litellm_api_base": litellm_api_base,
            "litellm_api_key": litellm_key,
            "error_message": error_message,
            "usage": usage,
            "budget": budget,
            "budget_source": budget_source,
            "budget_duration": getattr(settings, "LITELLM_KEY_BUDGET_DURATION", ""),
            "allowed_models": litellm_keys.models_for(request.user),
        },
    )


@login_required
@require_POST
def regenerate_key(request):
    """Rotate the user's key (e.g. after it leaked). Same limits, new secret."""
    user = request.user
    if not getattr(settings, "LITELLM_ENABLE_VIRTUAL_KEYS", False):
        messages.error(request, "API keys are not enabled.")
    elif not (user.is_staff or user.has_active_enrollment()):
        messages.error(request, "You are not enrolled in any active course.")
    else:
        try:
            litellm_keys.regenerate_key(user)
            messages.success(request, "Your API key was regenerated. Update it wherever the old key was used.")
        except Exception:
            logger.exception("Could not regenerate API key for %s", user.email)
            messages.error(request, "Could not regenerate your key right now. Please try again later.")
    return redirect("settings")
