"""Service layer for LiteLLM model information retrieval."""

import logging
from decimal import Decimal

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)


def _get_litellm_client_config():
    """Return (base_url, headers) for LiteLLM management API calls."""
    base_url = getattr(settings, "LITELLM_PROXY_BASE_URL", None)
    if not base_url:
        base_url = "http://localhost:8000/litellm"
    base_url = base_url.rstrip("/")

    master_key = getattr(settings, "LITELLM_MASTER_KEY", "")
    headers = {
        "Authorization": f"Bearer {master_key}",
        "Content-Type": "application/json",
    }
    return base_url, headers


def _format_cost(cost_per_token):
    """Convert cost-per-token to cost-per-million-tokens for readability."""
    if cost_per_token is None:
        return None
    return Decimal(str(cost_per_token)) * Decimal("1000000")


def get_model_info():
    """
    Fetch model info from LiteLLM's /model/info endpoint.
    Returns {"success": bool, "models": list[dict], "message": str}
    Each model dict has: name, provider, mode, max_input_tokens, max_output_tokens,
    input_cost_per_m, output_cost_per_m, and capabilities (list of strings).
    """
    base_url, headers = _get_litellm_client_config()
    try:
        with httpx.Client(timeout=httpx.Timeout(15.0, connect=5.0)) as client:
            resp = client.get(f"{base_url}/model/info", headers=headers)
            resp.raise_for_status()
            data = resp.json()

        raw_models = data.get("data", [])
        models = []
        for raw in raw_models:
            info = raw.get("model_info", {})
            params = raw.get("litellm_params", {})

            # Build capabilities list from supports_* flags
            capabilities = []
            capability_map = {
                "supports_vision": "Vision",
                "supports_function_calling": "Function Calling",
                "supports_response_schema": "Structured Output",
                "supports_reasoning": "Reasoning",
                "supports_prompt_caching": "Prompt Caching",
                "supports_audio_input": "Audio Input",
                "supports_audio_output": "Audio Output",
                "supports_pdf_input": "PDF Input",
                "supports_native_streaming": "Streaming",
                "supports_web_search": "Web Search",
            }
            for key, label in capability_map.items():
                if info.get(key):
                    capabilities.append(label)

            models.append({
                "name": raw.get("model_name", "unknown"),
                "provider": info.get("litellm_provider", "unknown"),
                "mode": info.get("mode", "chat"),
                "max_input_tokens": info.get("max_input_tokens"),
                "max_output_tokens": info.get("max_output_tokens"),
                "input_cost_per_m": _format_cost(info.get("input_cost_per_token")),
                "output_cost_per_m": _format_cost(info.get("output_cost_per_token")),
                "capabilities": capabilities,
            })

        # Sort by name
        models.sort(key=lambda m: m["name"])
        return {"success": True, "models": models, "message": ""}

    except Exception as exc:
        logger.exception("Failed to fetch model info")
        return {"success": False, "models": [], "message": "Could not fetch model info from the proxy. Please try again later."}


def model_names(extra=()):
    """
    Names of models the proxy exposes, for allowlist pickers. Falls back to
    the names in config/litellm-config.yaml if the proxy is unreachable, and
    always includes `extra` (already-selected values) so they stay selectable.
    """
    names = []
    result = get_model_info()
    if result["success"]:
        names = [m["name"] for m in result["models"]]
    if not names:
        try:
            import yaml
            from django.conf import settings as dj_settings
            path = dj_settings.BASE_DIR / "config" / "litellm-config.yaml"
            with open(path) as fh:
                names = [m["model_name"] for m in (yaml.safe_load(fh) or {}).get("model_list", [])]
        except Exception:
            logger.warning("Could not read model names from config", exc_info=True)
    return sorted(set(names) | set(extra or ()))
