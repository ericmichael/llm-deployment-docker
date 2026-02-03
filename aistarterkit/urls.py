"""
URL configuration for aistarterkit project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/4.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import include, path
from django.contrib.auth import views as auth_views
from django.views.generic import RedirectView
from django.http import JsonResponse
from django.db import connection
from chat.views import CustomLoginView


def health_check(request):
    """Health check endpoint for container orchestration."""
    health_status = {"status": "healthy", "checks": {}}

    # Check database connectivity
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        health_status["checks"]["database"] = "ok"
    except Exception as e:
        health_status["checks"]["database"] = f"error: {str(e)}"
        health_status["status"] = "unhealthy"

    # Check LiteLLM connectivity using the lightweight /health/liveliness endpoint.
    # LiteLLM is mounted at /litellm/* in the unified ASGI app.
    try:
        import requests
        resp = requests.get("http://localhost:8000/litellm/health/liveliness", timeout=5)
        if resp.status_code == 200:
            health_status["checks"]["litellm"] = "ok"
        else:
            health_status["checks"]["litellm"] = f"error: status {resp.status_code}"
            health_status["status"] = "degraded"
    except Exception as e:
        health_status["checks"]["litellm"] = f"error: {str(e)}"
        health_status["status"] = "degraded"

    # Return 200 even when degraded so the container orchestrator doesn't restart
    # the container due to a temporarily slow LiteLLM process. Only return 503
    # when the Django app itself is unhealthy (e.g. database down).
    status_code = 200 if health_status["status"] != "unhealthy" else 503
    return JsonResponse(health_status, status=status_code)


urlpatterns = [
    path('health/', health_check, name='health_check'),
    path('', RedirectView.as_view(pattern_name='settings', permanent=False)),  # Redirect to settings
    path('login/', CustomLoginView.as_view(), name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('admin/', admin.site.urls),
    path('chat/', include('chat.urls')),
]
