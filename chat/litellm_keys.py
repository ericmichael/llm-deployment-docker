"""
LiteLLM virtual key operations: generation, limits, revocation.

All student keys are created with spend/rate limits and an expiry so a
runaway loop (or a leaked key) has bounded blast radius. Limits come from
settings (env-overridable); a limit set to 0/empty is omitted entirely.
"""

import logging
from datetime import datetime, timezone

import httpx
from django.conf import settings
from django.db import transaction
from django.utils import timezone as dj_timezone

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


def effective_budget(user):
    """
    Resolve the user's spend cap (USD per LITELLM_KEY_BUDGET_DURATION).

    Precedence: user override -> budget of the course they're a student in
    (or any active course with a budget set) -> global LITELLM_KEY_MAX_BUDGET.
    Returns a float; 0 means unlimited. `source` is exposed for the UI via
    effective_budget_with_source().
    """
    return effective_budget_with_source(user)[0]


def effective_budget_with_source(user):
    if user is not None:
        if getattr(user, "monthly_budget", None) is not None:
            return float(user.monthly_budget), "user"
        if user.pk:
            # Prefer the course that owns the key (same choice as primary_course),
            # then any other active course that sets a budget.
            course = primary_course(user)
            if course is None or course.monthly_budget is None:
                enrollment = (
                    user.enrollments.filter(course__is_active=True, course__monthly_budget__isnull=False)
                    .select_related("course")
                    .order_by("role", "course_id")
                    .first()
                )
                course = enrollment.course if enrollment else None
            if course is not None and course.monthly_budget is not None:
                return float(course.monthly_budget), f"course {course.code}"
    return float(getattr(settings, "LITELLM_KEY_MAX_BUDGET", 0) or 0), "default"


def primary_course(user):
    """The active course that governs a user's key: student enrollment first, then TA."""
    if user is None or not user.pk:
        return None
    enrollment = (
        user.enrollments.filter(course__is_active=True)
        .select_related("course")
        .order_by("role", "course_id")  # "student" < "ta"; oldest course breaks TA ties
        .first()
    )
    return enrollment.course if enrollment else None


def models_for(user) -> list:
    """Model allowlist for a user's key ([] = every model the proxy exposes)."""
    course = primary_course(user)
    if course is not None and course.allowed_models:
        return list(course.allowed_models)
    return list(getattr(settings, "LITELLM_KEY_DEFAULT_MODELS", []) or [])


def team_for(user) -> str:
    """LiteLLM team id for a user's key ("" if their course has no team yet)."""
    course = primary_course(user)
    return course.litellm_team_id if course is not None else ""


def ensure_team_member(team_id: str, email: str) -> None:
    """
    Add the user to the team (idempotent). /key/update refuses to move a key
    into a team unless the key's user is a member, so this runs before every
    scoped key update.
    """
    with httpx.Client(timeout=TIMEOUT) as client:
        resp = client.post(
            f"{base_url()}/team/member_add",
            headers=master_headers(),
            json={"team_id": team_id, "member": {"user_id": email, "role": "user"}},
        )
    if resp.status_code >= 400 and "already in team" not in resp.text.lower():
        raise RuntimeError(f"LiteLLM team member add failed for {email}: {resp.status_code} {resp.text[:256]}")


def key_scope_payload(user) -> dict:
    """team_id / models fields for /key/generate and /key/update."""
    payload = {"models": models_for(user)}
    team_id = team_for(user)
    if team_id:
        payload["team_id"] = team_id
    return payload


def key_limit_payload(user=None) -> dict:
    """The budget/rate/expiry fields applied to a key (global limits + the user's effective budget)."""
    payload = {}

    max_budget = effective_budget(user)
    if max_budget:
        payload["max_budget"] = max_budget
        budget_duration = getattr(settings, "LITELLM_KEY_BUDGET_DURATION", None)
        if budget_duration:
            payload["budget_duration"] = budget_duration
    else:
        payload["max_budget"] = None  # explicit: clears a previous cap on /key/update

    rpm = getattr(settings, "LITELLM_KEY_RPM_LIMIT", None)
    if rpm:
        payload["rpm_limit"] = int(rpm)

    tpm = getattr(settings, "LITELLM_KEY_TPM_LIMIT", None)
    if tpm:
        payload["tpm_limit"] = int(tpm)

    return payload


def key_expiry_payload() -> dict:
    """Expiry is set only when a key is issued; re-sending `duration` on
    /key/update would push the expiry out again on every sync."""
    duration = getattr(settings, "LITELLM_KEY_DURATION", None)
    return {"duration": duration} if duration else {}


def _parse_expires(value):
    """Parse LiteLLM's ISO-8601 `expires` into an aware datetime (or None)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def generate_key(user, spend: float | None = None) -> tuple[str, str, "datetime | None"]:
    """Create a virtual key for a user. Returns (key, key_id, expires).

    `spend` seeds the new key's current-period counter (used when rotating a
    key so the student doesn't get a fresh budget).
    """
    email = getattr(user, "email", "user")
    scope = key_scope_payload(user)
    if scope.get("team_id"):
        ensure_team_member(scope["team_id"], email)
    payload = {
        "key_alias": email,
        "user_id": email,
        "metadata": {"django_user_id": str(user.id), "email": email},
        **scope,
        **key_limit_payload(user),
        **key_expiry_payload(),
    }
    if spend:
        payload["spend"] = float(spend)
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
    return key, str(key_id) if key_id else "", _parse_expires(data.get("expires"))


def key_info(key: str) -> "dict | None":
    """Look up a key at the proxy. Returns the info dict, or None if it no longer exists."""
    with httpx.Client(timeout=TIMEOUT) as client:
        resp = client.get(f"{base_url()}/key/info", headers=master_headers(), params={"key": key})
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        raise RuntimeError(f"LiteLLM key lookup failed: {resp.status_code} {resp.text[:256]}")
    return resp.json().get("info") or {}


def _key_is_usable(user) -> bool:
    """
    Whether the user's stored key still exists at the proxy and hasn't
    expired. Always asks the proxy so a key deleted out-of-band (LiteLLM UI,
    rolled-back course delete, admin /key/delete) is replaced instead of
    being shown as valid.
    """
    if not user.litellm_key:
        return False
    info = key_info(user.litellm_key)
    if info is None:
        return False
    now = dj_timezone.now()
    expires = _parse_expires(info.get("expires"))
    if expires != user.litellm_key_expires:
        user.litellm_key_expires = expires
        user.save(update_fields=["litellm_key_expires"])
    return expires is None or expires > now


def ensure_key(user) -> str:
    """
    Return the user's virtual key, creating or renewing it if needed.

    Serialized per user with a row lock so two concurrent first visits can't
    each generate a key and leave the DB pointing at one the proxy deleted.
    """
    from django.contrib.auth import get_user_model  # request.user may be a lazy proxy

    model = get_user_model()
    with transaction.atomic():
        locked = model.objects.select_for_update().get(pk=user.pk)
        if _key_is_usable(locked):
            key = locked.litellm_key
        else:
            key, key_id, expires = generate_key(locked)
            locked.litellm_key = key
            locked.litellm_key_id = key_id
            locked.litellm_key_expires = expires
            locked.save(update_fields=["litellm_key", "litellm_key_id", "litellm_key_expires"])
    user.litellm_key = locked.litellm_key
    user.litellm_key_id = locked.litellm_key_id
    user.litellm_key_expires = locked.litellm_key_expires
    return key


def regenerate_key(user) -> str:
    """
    Rotate the user's key: the old secret stops working immediately and a new
    one is issued with the same limits/team/models and the same current-period
    spend (so rotating never grants a fresh budget).

    (LiteLLM's native /key/regenerate is enterprise-only, so this is
    delete + generate, carrying the spend counter across.)
    """
    from django.contrib.auth import get_user_model

    model = get_user_model()
    with transaction.atomic():
        locked = model.objects.select_for_update().get(pk=user.pk)
        spend = 0.0
        if locked.litellm_key:
            info = key_info(locked.litellm_key)
            if info is not None:
                spend = float(info.get("spend") or 0)
                delete_key_strict(locked.litellm_key)  # a leaked key must really die
            locked.litellm_key = ""
            locked.litellm_key_id = ""
            locked.litellm_key_expires = None
            locked.save(update_fields=["litellm_key", "litellm_key_id", "litellm_key_expires"])
    # The old key is gone at the proxy and cleared locally (committed above), so
    # if issuing the new one fails the user simply has no key until the next
    # settings visit re-provisions - never a dead key that looks valid.
    with transaction.atomic():
        locked = model.objects.select_for_update().get(pk=user.pk)
        key, key_id, expires = generate_key(locked, spend=spend)
        locked.litellm_key = key
        locked.litellm_key_id = key_id
        locked.litellm_key_expires = expires
        locked.save(update_fields=["litellm_key", "litellm_key_id", "litellm_key_expires"])
    user.litellm_key = key
    user.litellm_key_id = key_id
    user.litellm_key_expires = expires
    return key


# --- Teams: one LiteLLM team per course (course-wide budget + model allowlist)

def team_payload(course) -> dict:
    payload = {
        "team_alias": course.code,
        "models": list(course.allowed_models or []),
        "metadata": {"django_course_id": str(course.pk), "name": course.name, "semester": course.semester},
    }
    total = course.total_budget
    if total is not None and float(total) > 0:
        payload["max_budget"] = float(total)
        duration = getattr(settings, "LITELLM_KEY_BUDGET_DURATION", None)
        if duration:
            payload["budget_duration"] = duration
    else:
        payload["max_budget"] = None
    return payload


def ensure_team(course) -> str:
    """Create or update the course's LiteLLM team. Returns the team id."""
    payload = team_payload(course)
    with httpx.Client(timeout=TIMEOUT) as client:
        if course.litellm_team_id:
            resp = client.post(
                f"{base_url()}/team/update",
                headers=master_headers(),
                json={"team_id": course.litellm_team_id, **payload},
            )
            if resp.status_code < 400:
                return course.litellm_team_id
            if resp.status_code != 404:
                raise RuntimeError(f"LiteLLM team update failed for {course.code}: {resp.status_code} {resp.text[:256]}")
            # team vanished at the proxy: fall through and recreate
        resp = client.post(f"{base_url()}/team/new", headers=master_headers(), json=payload)
    if resp.status_code >= 400:
        raise RuntimeError(f"LiteLLM team create failed for {course.code}: {resp.status_code} {resp.text[:256]}")
    team_id = resp.json().get("team_id")
    if not team_id:
        raise RuntimeError("LiteLLM did not return a team_id")
    type(course).objects.filter(pk=course.pk).update(litellm_team_id=team_id)
    course.litellm_team_id = team_id
    return team_id


def members_keyed_to(course):
    """Users whose key belongs to this course's team (their primary course is this one)."""
    from django.contrib.auth import get_user_model

    users = get_user_model().objects.filter(enrollments__course=course).exclude(litellm_key="").distinct()
    return [u for u in users if primary_course(u) == course]


def clear_local_keys(users) -> int:
    """Forget local key state for users (proxy side handled by the caller)."""
    from django.contrib.auth import get_user_model

    return get_user_model().objects.filter(pk__in=[u.pk for u in users]).update(
        litellm_key="", litellm_key_id="", litellm_key_expires=None
    )


def delete_team_at_proxy(team_id: str) -> None:
    """Delete a team (LiteLLM also deletes every key in it). Logged, never raised."""
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.post(f"{base_url()}/team/delete", headers=master_headers(), json={"team_ids": [team_id]})
        if resp.status_code >= 400 and resp.status_code != 404:
            logger.warning("LiteLLM team delete %s returned %s: %s", team_id, resp.status_code, resp.text[:256])
    except (httpx.HTTPError, RuntimeError) as exc:
        logger.warning("LiteLLM team delete %s failed: %s", team_id, exc)


def delete_team(course) -> bool:
    """
    Course is being deleted: forget the local keys that live in its team
    (LiteLLM deletes them with the team) and delete the team at the proxy.
    Members whose key belongs to another course's team are left alone.
    """
    team_id = course.litellm_team_id
    if not team_id:
        return False
    clear_local_keys(members_keyed_to(course))
    delete_team_at_proxy(team_id)
    return True


def team_usage(course):
    """Course-wide spend vs cap from /team/info, or None if unavailable."""
    if not course.litellm_team_id:
        return None
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.get(
                f"{base_url()}/team/info",
                headers=master_headers(),
                params={"team_id": course.litellm_team_id, "key_limit": 1},
            )
        if resp.status_code >= 400:
            return None
        info = resp.json().get("team_info") or {}
    except (httpx.HTTPError, RuntimeError, ValueError):
        logger.warning("Could not fetch team usage for %s", course.code, exc_info=True)
        return None
    return {
        "spend": float(info.get("spend") or 0),
        "max_budget": info.get("max_budget"),
        "budget_reset_at": _parse_expires(info.get("budget_reset_at")),
    }


def delete_key_strict(key: str) -> None:
    """Delete a key at the proxy; raises unless it is gone afterwards."""
    with httpx.Client(timeout=TIMEOUT) as client:
        resp = client.post(f"{base_url()}/key/delete", headers=master_headers(), json={"keys": [key]})
    already_gone = resp.status_code == 404 or (
        resp.status_code == 400 and ("not all keys" in resp.text.lower() or "no keys found" in resp.text.lower())
    )
    if resp.status_code >= 400 and not already_gone:
        raise RuntimeError(f"LiteLLM key delete failed: {resp.status_code} {resp.text[:256]}")


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
        # Already gone at the proxy is the outcome we wanted (404, or 400
        # "Not all keys passed in were deleted" / "No keys found").
        already_gone = resp.status_code == 404 or (
            resp.status_code == 400 and ("not all keys" in resp.text.lower() or "no keys found" in resp.text.lower())
        )
        if resp.status_code >= 400 and not already_gone:
            logger.warning(
                "LiteLLM key delete for %s returned %s: %s",
                user.email, resp.status_code, resp.text[:256],
            )
    except (httpx.HTTPError, RuntimeError) as exc:
        logger.warning("LiteLLM key delete for %s failed: %s", user.email, exc)

    user.litellm_key = ""
    user.litellm_key_id = ""
    user.litellm_key_expires = None
    user.save(update_fields=["litellm_key", "litellm_key_id", "litellm_key_expires"])
    return True


def revoke_if_unentitled(user) -> bool:
    """Revoke the user's key unless they're staff or still actively enrolled."""
    if user_keeps_key(user):
        return False
    return revoke_key(user)


def update_key_limits(user) -> bool:
    """
    Re-sync a user's existing key with the proxy: spend attribution
    (user_id/metadata) and the current effective limits, in one /key/update.
    Returns False if the user has no key.
    """
    key = getattr(user, "litellm_key", "")
    if not key:
        return False
    limits = key_limit_payload(user)
    email = user.email
    scope = key_scope_payload(user)
    current = key_info(key) or {}
    # LiteLLM recomputes budget_reset_at whenever budget_duration is sent;
    # around the 1st that can skip a reset, so only send it when it changes.
    if limits.get("budget_duration") == current.get("budget_duration"):
        limits.pop("budget_duration", None)
    if scope.get("team_id"):
        ensure_team_member(scope["team_id"], email)

    with httpx.Client(timeout=TIMEOUT) as client:
        if scope.get("team_id"):
            # LiteLLM validates the key's *stored* model list against the
            # *new* team when team_id changes, so a move is three steps:
            # clear models -> change team -> apply the new allowlist (below).
            if current.get("team_id") != scope["team_id"]:
                for step, body in (("clear models", {"models": []}), ("move team", {"team_id": scope["team_id"]})):
                    move = client.post(
                        f"{base_url()}/key/update", headers=master_headers(), json={"key": key, **body}
                    )
                    if move.status_code >= 400:
                        raise RuntimeError(
                            f"LiteLLM key {step} failed for {email}: {move.status_code} {move.text[:256]}"
                        )
        resp = client.post(
            f"{base_url()}/key/update",
            headers=master_headers(),
            json={
                "key": key,
                "user_id": email,
                "metadata": {"django_user_id": str(user.id), "email": email},
                **scope,
                **limits,
            },
        )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"LiteLLM key update failed for {user.email}: {resp.status_code} {resp.text[:256]}"
        )
    return True


sync_key = update_key_limits  # readable alias for callers that re-sync after a budget change


def reset_spend(user) -> bool:
    """
    Zero the spend counter on the user's key so they can use the API again
    before the scheduled monthly reset. Historical SpendLogs are untouched.
    Returns False if the user has no key.
    """
    key = getattr(user, "litellm_key", "")
    if not key:
        return False
    with httpx.Client(timeout=TIMEOUT) as client:
        resp = client.post(
            f"{base_url()}/key/update",
            headers=master_headers(),
            json={"key": key, "spend": 0},
        )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"LiteLLM spend reset failed for {user.email}: {resp.status_code} {resp.text[:256]}"
        )
    return True


def reset_spend_for_users(users) -> dict:
    """Reset spend for each user with a key. Returns {"reset": n, "failed": [emails]}."""
    result = {"reset": 0, "failed": []}
    for user in users:
        try:
            if reset_spend(user):
                result["reset"] += 1
        except (httpx.HTTPError, RuntimeError) as exc:
            logger.warning("Spend reset failed for %s: %s", user.email, exc)
            result["failed"].append(user.email)
    return result


def key_usage(user):
    """
    Current spend vs cap for the user's key, for display.
    Returns dict(spend, max_budget, budget_reset_at) or None if unavailable.
    """
    key = getattr(user, "litellm_key", "")
    if not key:
        return None
    try:
        info = key_info(key)
    except (httpx.HTTPError, RuntimeError):
        logger.warning("Could not fetch key usage for %s", user.email, exc_info=True)
        return None
    if info is None:
        return None
    return {
        "spend": float(info.get("spend") or 0),
        "max_budget": info.get("max_budget"),
        "budget_reset_at": _parse_expires(info.get("budget_reset_at")),
    }


def user_keeps_key(user) -> bool:
    """Staff and users with an active enrollment keep their keys."""
    return user.is_staff or user.has_active_enrollment()
