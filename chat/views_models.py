"""Views for model information page."""

from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from . import litellm_keys, services_models


@login_required
def model_list(request):
    """Display available LLM models and their capabilities."""
    result = services_models.get_model_info()
    allowed = set(litellm_keys.models_for(request.user))
    models = result["models"]
    for m in models:
        m["allowed"] = not allowed or m["name"] in allowed
    models.sort(key=lambda m: (not m["allowed"], m["name"]))

    return render(request, "models/model_list.html", {
        "models": models,
        "restricted": bool(allowed),
        "error": result["message"] if not result["success"] else "",
    })
