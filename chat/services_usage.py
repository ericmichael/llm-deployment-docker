"""Service layer for LiteLLM usage/spend data retrieval."""

import logging
from datetime import date, timedelta
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


def parse_date_range(range_key, custom_start=None, custom_end=None):
    """Convert a range key to (start_date, end_date) as date objects."""
    today = date.today()
    if range_key == "today":
        return today, today
    elif range_key == "week":
        return today - timedelta(days=7), today
    elif range_key == "custom" and custom_start and custom_end:
        return custom_start, custom_end
    else:  # default: "month"
        return today - timedelta(days=30), today


def get_spend_logs(start_date, end_date):
    """
    Fetch daily spend logs from LiteLLM.
    Returns {"success": bool, "logs": list[dict], "message": str}

    Each log entry is a daily summary with:
      - startTime: date string
      - spend: total spend for the day
      - users: {user_id: spend} dict
      - models: {model_name: spend} dict
    """
    base_url, headers = _get_litellm_client_config()
    try:
        with httpx.Client(timeout=httpx.Timeout(30.0, connect=5.0)) as client:
            resp = client.get(
                f"{base_url}/spend/logs",
                headers=headers,
                params={
                    "start_date": str(start_date),
                    "end_date": str(end_date + timedelta(days=1)),
                },
            )
            resp.raise_for_status()
            data = resp.json()
        logs = data if isinstance(data, list) else data.get("logs", data.get("data", []))
        return {"success": True, "logs": logs, "message": ""}
    except Exception as exc:
        logger.exception("Failed to fetch spend logs")
        return {"success": False, "logs": [], "message": f"Could not fetch spend logs: {exc}"}


def aggregate_from_logs(logs, filter_emails=None):
    """
    Aggregate daily spend log summaries into by-user and by-model breakdowns.

    Args:
        logs: List of daily summary dicts from LiteLLM /spend/logs
        filter_emails: Optional set/list of emails to filter users by (for course filtering).
                       When set, only matching users are included and total_spend is
                       computed from their spend only.

    Returns dict with:
        total_spend, by_user (list), by_model (list)
    """
    email_set = set(filter_emails) if filter_emails is not None else None

    by_user = {}
    by_model = {}
    total_spend = Decimal("0")

    for log in logs:
        # Aggregate users
        users = log.get("users") or {}
        for user_id, spend in users.items():
            if email_set is not None and user_id not in email_set:
                continue
            spend_d = Decimal(str(spend or 0))
            if user_id not in by_user:
                by_user[user_id] = {
                    "email": user_id or "unknown",
                    "total_spend": Decimal("0"),
                }
            by_user[user_id]["total_spend"] += spend_d
            total_spend += spend_d

        # Aggregate models (only if no filter, or if we're including some users from this day)
        if email_set is None:
            models = log.get("models") or {}
            for model_name, spend in models.items():
                spend_d = Decimal(str(spend or 0))
                if model_name not in by_model:
                    by_model[model_name] = {
                        "model": _friendly_model_name(model_name),
                        "total_spend": Decimal("0"),
                    }
                by_model[model_name]["total_spend"] += spend_d

    # When filtering by course, we can't break down by model from the daily summaries
    # (the API doesn't give per-user-per-model data), so we skip model breakdown

    if email_set is None:
        total_spend = sum((m["total_spend"] for m in by_model.values()), Decimal("0"))

    by_user_list = sorted(by_user.values(), key=lambda x: x["total_spend"], reverse=True)
    by_model_list = sorted(by_model.values(), key=lambda x: x["total_spend"], reverse=True)

    return {
        "total_spend": total_spend,
        "by_user": by_user_list,
        "by_model": by_model_list,
    }


def _friendly_model_name(model_name):
    """Strip provider prefix from model name (e.g. 'azure/gpt-5' -> 'gpt-5')."""
    if "/" in model_name:
        return model_name.split("/", 1)[1]
    return model_name
