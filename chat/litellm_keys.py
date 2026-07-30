"""
LiteLLM virtual key operations: generation, limits, revocation.

All student keys are created with spend/rate limits and an expiry so a
runaway loop (or a leaked key) has bounded blast radius. Limits come from
settings (env-overridable); a limit set to 0/empty is omitted entirely.
"""

import logging

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(10.0, connect=5.0)


def base_url() -> str:
    url = getattr(settings, "LITELLM_PROXY_BASE_URL", None) or "http://localhost:8000/litellm"
    return url.rstrip("/")


def master_headers() -> dict:
    master_key = getattr(settings, "LITELLM_MASTER_KEY", None)
    if not master_key:
        raise RuntimeError("LITELLM_MASTER_KEY not set")
    return {"Authorization": f"Bearer {master_key}", "Content-Type": "application/json"}


def key_limit_payload() -> dict:
    """The budget/rate/expiry fields applied to every student key."""
    payload = {}

    max_budget = getattr(settings, "LITELLM_KEY_MAX_BUDGET", None)
    if max_budget:
        payload["max_budget"] = float(max_budget)
        budget_duration = getattr(settings, "LITELLM_KEY_BUDGET_DURATION", None)
        if budget_duration:
            payload["budget_duration"] = budget_duration

    rpm = getattr(settings, "LITELLM_KEY_RPM_LIMIT", None)
    if rpm:
        payload["rpm_limit"] = int(rpm)

    tpm = getattr(settings, "LITELLM_KEY_TPM_LIMIT", None)
    if tpm:
        payload["tpm_limit"] = int(tpm)

    duration = getattr(settings, "LITELLM_KEY_DURATION", None)
    if duration:
        payload["duration"] = duration

    return payload


def generate_key(user) -> tuple[str, str]:
    """Create a virtual key for a user. Returns (key, key_id)."""
    email = getattr(user, "email", "user")
    payload = {
        "key_alias": email,
        "user_id": email,
        "models": [],
        "metadata": {"django_user_id": str(user.id), "email": email},
        **key_limit_payload(),
    }
    headers = master_headers()

    with httpx.Client(timeout=TIMEOUT) as client:
        resp = client.post(f"{base_url()}/key/generate", headers=headers, json=payload)

        # If alias already exists, delete the old key and retry
        if resp.status_code == 400 and "already exists" in resp.text:
            client.post(
                f"{base_url()}/key/delete",
                headers=headers,
                json={"key_aliases": [email]},
            )
            resp = client.post(f"{base_url()}/key/generate", headers=headers, json=payload)

    if resp.status_code >= 400:
        raise RuntimeError(f"LiteLLM key generation failed: {resp.status_code} {resp.text[:512]}")

    data = resp.json()
    key = data.get("key") or data.get("token")
    key_id = data.get("key_id") or data.get("token_id") or data.get("id")
    if not key:
        raise RuntimeError("LiteLLM did not return a key")
    return key, str(key_id) if key_id else ""


def revoke_key(user) -> bool:
    """
    Delete a user's virtual key at the proxy and clear it locally.

    Returns True if a key was revoked. LiteLLM errors are logged, not
    raised - a dangling remote key is better than blocking roster changes,
    and revoke_unenrolled_keys can sweep up stragglers later.
    """
    key = getattr(user, "litellm_key", "")
    if not key:
        return False

    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.post(
                f"{base_url()}/key/delete",
                headers=master_headers(),
                json={"keys": [key]},
            )
        if resp.status_code >= 400 and "not found" not in resp.text.lower():
            logger.warning(
                "LiteLLM key delete for %s returned %s: %s",
                user.email, resp.status_code, resp.text[:256],
            )
    except (httpx.HTTPError, RuntimeError) as exc:
        logger.warning("LiteLLM key delete for %s failed: %s", user.email, exc)

    user.litellm_key = ""
    user.litellm_key_id = ""
    user.save(update_fields=["litellm_key", "litellm_key_id"])
    return True


def update_key_limits(user) -> bool:
    """Apply the current limit settings to a user's existing key."""
    key = getattr(user, "litellm_key", "")
    limits = key_limit_payload()
    if not key or not limits:
        return False

    with httpx.Client(timeout=TIMEOUT) as client:
        resp = client.post(
            f"{base_url()}/key/update",
            headers=master_headers(),
            json={"key": key, **limits},
        )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"LiteLLM key update failed for {user.email}: {resp.status_code} {resp.text[:256]}"
        )
    return True


def user_keeps_key(user) -> bool:
    """Staff and users with an active enrollment keep their keys."""
    return user.is_staff or user.has_active_enrollment()
