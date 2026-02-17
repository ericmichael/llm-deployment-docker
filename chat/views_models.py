"""Views for model information page."""

from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from . import services_models


@login_required
def model_list(request):
    """Display available LLM models and their capabilities."""
    result = services_models.get_model_info()

    return render(request, "models/model_list.html", {
        "models": result["models"],
        "error": result["message"] if not result["success"] else "",
    })
