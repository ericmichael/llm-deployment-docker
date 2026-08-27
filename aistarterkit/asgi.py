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


def _register_base_model_prices():
    """
    Make every deployment's `litellm_params.model` resolvable in LiteLLM's
    price map when the config declares `model_info.base_model`.

    LiteLLM applies base_model for chat cost tracking, but the realtime cost
    path (handle_realtime_stream_cost_calculation) only tries the session's
    model name and the deployment name - so `azure/gpt-realtime` priced as $0
    until it exists in litellm.model_cost. Aliasing it to the base model's
    entry fixes that without patching LiteLLM.
    """
    import litellm
    import yaml

    config_path = os.getenv("CONFIG_FILE_PATH")
    if not config_path or not os.path.exists(config_path):
        return
    with open(config_path) as fh:
        config = yaml.safe_load(fh) or {}
    aliases = {}
    for entry in config.get("model_list", []):
        base = ((entry.get("model_info") or {}).get("base_model"))
        deployment = (entry.get("litellm_params") or {}).get("model")
        if not base or not deployment or deployment == base:
            continue
        price = litellm.model_cost.get(base)
        if price is None:
            continue
        for name in (deployment, deployment.split("/", 1)[-1]):
            if name not in litellm.model_cost:
                aliases[name] = dict(price)
    if aliases:
        litellm.register_model(aliases)


def _get_litellm_asgi_app():
    litellm_database_url = os.getenv("LITELLM_DATABASE_URL")
    if litellm_database_url:
        os.environ["DATABASE_URL"] = litellm_database_url

    import litellm.proxy.proxy_server as proxy_server

    _register_base_model_prices()
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
