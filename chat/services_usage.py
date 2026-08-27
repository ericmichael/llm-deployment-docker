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


def get_daily_activity(start_date, end_date):
    """
    Fetch per-day spend from LiteLLM's aggregated daily table
    (/user/daily/activity, admin/global view). Pages through all results.

    Returns {"success": bool, "days": list[dict], "message": str}. Each day dict:
      {"date": "YYYY-MM-DD", "spend": float, "requests": int, "tokens": int,
       "users": {user_id: {"spend", "requests", "tokens"}},
       "models": {model: {"spend", "requests", "tokens"}}}
    """
    base_url, headers = _get_litellm_client_config()
    days = []
    try:
        with httpx.Client(timeout=httpx.Timeout(30.0, connect=5.0)) as client:
            page = 1
            while True:
                resp = client.get(
                    f"{base_url}/user/daily/activity",
                    headers=headers,
                    params={
                        "start_date": str(start_date),
                        "end_date": str(end_date),
                        "page": page,
                        "page_size": 1000,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                for row in data.get("results", []):
                    days.append(_normalize_day(row))
                meta = data.get("metadata", {}) or {}
                if not meta.get("has_more") and page >= int(meta.get("total_pages") or 1):
                    break
                page += 1
                if page > 100:  # safety valve
                    logger.warning("Daily activity pagination exceeded 100 pages; truncating")
                    break
        return {"success": True, "days": days, "message": ""}
    except Exception:
        logger.exception("Failed to fetch daily activity")
        return {"success": False, "days": [], "message": "Could not fetch usage from the proxy. Please try again later."}


def _metrics(block):
    m = (block or {}).get("metrics", block) or {}
    return {
        "spend": float(m.get("spend") or 0),
        "requests": int(m.get("api_requests") or 0),
        "tokens": int(m.get("total_tokens") or 0),
    }


def _normalize_day(row):
    breakdown = row.get("breakdown", {}) or {}
    users = {}
    # `entities` is keyed by user_id on the user daily table; fall back to key alias (= email).
    for user_id, block in (breakdown.get("entities") or {}).items():
        users[user_id or ""] = _metrics(block)
    if not users:
        for _, block in (breakdown.get("api_keys") or {}).items():
            alias = ((block or {}).get("metadata") or {}).get("key_alias") or ""
            cur = users.setdefault(alias, {"spend": 0.0, "requests": 0, "tokens": 0})
            for k, v in _metrics(block).items():
                cur[k] += v
    models = {name: _metrics(block) for name, block in (breakdown.get("models") or {}).items()}
    top = _metrics(row)
    return {"date": str(row.get("date")), **top, "users": users, "models": models}


def aggregate_from_days(days, filter_emails=None):
    """
    Aggregate normalized day dicts into totals and by-user / by-model lists.
    filter_emails restricts users (course filter); model breakdown is only
    available for the unfiltered view (the daily table isn't per-user-per-model).
    """
    email_set = set(filter_emails) if filter_emails is not None else None
    by_user, by_model = {}, {}
    total = {"spend": Decimal("0"), "requests": 0, "tokens": 0}

    for day in days:
        for user_id, m in day["users"].items():
            if email_set is not None and user_id not in email_set:
                continue
            row = by_user.setdefault(user_id, {"email": user_id or "unknown", "total_spend": Decimal("0"), "requests": 0, "tokens": 0})
            row["total_spend"] += Decimal(str(m["spend"]))
            row["requests"] += m["requests"]
            row["tokens"] += m["tokens"]
            if email_set is not None:
                total["spend"] += Decimal(str(m["spend"]))
                total["requests"] += m["requests"]
                total["tokens"] += m["tokens"]
        if email_set is None:
            for model_name, m in day["models"].items():
                friendly = _friendly_model_name(model_name)
                row = by_model.setdefault(friendly, {"model": friendly, "total_spend": Decimal("0"), "requests": 0, "tokens": 0})
                row["total_spend"] += Decimal(str(m["spend"]))
                row["requests"] += m["requests"]
                row["tokens"] += m["tokens"]
            total["spend"] += Decimal(str(day["spend"]))
            total["requests"] += day["requests"]
            total["tokens"] += day["tokens"]

    return {
        "total_spend": total["spend"],
        "total_requests": total["requests"],
        "total_tokens": total["tokens"],
        "by_user": sorted(by_user.values(), key=lambda x: x["total_spend"], reverse=True),
        "by_model": sorted(by_model.values(), key=lambda x: x["total_spend"], reverse=True),
    }


def _friendly_model_name(model_name):
    """Strip provider prefix from model name (e.g. 'azure/gpt-5' -> 'gpt-5')."""
    if "/" in model_name:
        return model_name.split("/", 1)[1]
    return model_name
