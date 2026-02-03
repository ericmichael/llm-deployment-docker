"""
ASGI config for aistarterkit project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/4.2/howto/deployment/asgi/
"""

import os

from django.core.asgi import get_asgi_application
from starlette.applications import Starlette
from starlette.routing import Mount
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack
from django.urls import re_path

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'aistarterkit.settings')

# Initialize Django ASGI application early to ensure the AppRegistry
# is populated before importing code that may import ORM models.
django_asgi_app = get_asgi_application()

from chat.routing import websocket_urlpatterns


def _get_litellm_asgi_app():
    litellm_database_url = os.getenv("LITELLM_DATABASE_URL")
    if litellm_database_url:
        os.environ["DATABASE_URL"] = litellm_database_url

    import litellm.proxy.proxy_server as proxy_server

    return proxy_server.app


litellm_asgi_app = _get_litellm_asgi_app()


http_app = Starlette(
    routes=[
        Mount("/v1", app=litellm_asgi_app),
        Mount("/litellm", app=litellm_asgi_app),
        Mount("/", app=django_asgi_app),
    ]
)

application = ProtocolTypeRouter({
    "http": http_app,
    "websocket": AuthMiddlewareStack(
        URLRouter(
            [
                re_path(r"^v1/realtime/?$", litellm_asgi_app),
                re_path(r"^realtime/?$", litellm_asgi_app),
                *websocket_urlpatterns,
            ]
        )
    ),
    "lifespan": litellm_asgi_app,
})
