"""Staff-only usage dashboard views."""

from datetime import datetime

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from django.contrib.auth import get_user_model

from . import litellm_keys, services_usage
from .models import Course, Enrollment

User = get_user_model()


@staff_member_required
def usage_dashboard(request):
    """Usage dashboard with spend overview, per-user, and per-model breakdowns."""
    range_key = request.GET.get("range", "month")
    custom_start = None
    custom_end = None

    if range_key == "custom":
        try:
            custom_start = datetime.strptime(request.GET.get("start", ""), "%Y-%m-%d").date()
            custom_end = datetime.strptime(request.GET.get("end", ""), "%Y-%m-%d").date()
        except (ValueError, TypeError):
            range_key = "month"

    start_date, end_date = services_usage.parse_date_range(range_key, custom_start, custom_end)

    # Course filter
    courses = Course.objects.all()
    course_id = request.GET.get("course", "")
    selected_course = None
    filter_emails = None
    if course_id:
        try:
            selected_course = Course.objects.get(id=int(course_id))
            filter_emails = list(
                Enrollment.objects.filter(course=selected_course)
                .values_list("user__email", flat=True)
            )
        except (Course.DoesNotExist, ValueError):
            pass

    errors = []

    activity = services_usage.get_daily_activity(start_date, end_date)
    if not activity["success"]:
        errors.append(activity["message"])

    agg = services_usage.aggregate_from_days(activity["days"], filter_emails=filter_emails)

    # Attach each user's effective budget so staff can see who is near their cap
    users_by_email = {
        u.email: u for u in User.objects.filter(email__in=[row["email"] for row in agg["by_user"]])
    }
    for row in agg["by_user"]:
        user = users_by_email.get(row["email"])
        row["budget"] = litellm_keys.effective_budget(user) if user else None
        row["budget_pct"] = (
            float(row["total_spend"]) / row["budget"] * 100 if row["budget"] else None
        )

    return render(request, "usage/dashboard.html", {
        "total_spend": agg["total_spend"],
        "total_requests": agg["total_requests"],
        "total_tokens": agg["total_tokens"],
        "total_users": len(agg["by_user"]),
        "by_user": agg["by_user"],
        "by_model": agg["by_model"],
        "range": range_key,
        "start_date": start_date,
        "end_date": end_date,
        "errors": errors,
        "courses": courses,
        "selected_course": selected_course,
    })


@staff_member_required
@require_POST
def usage_reset_all(request):
    """Zero the spend counter on every virtual key (global reset)."""
    users = User.objects.exclude(litellm_key="")
    result = litellm_keys.reset_spend_for_users(users)
    if result["failed"]:
        messages.warning(request, f"Reset {result['reset']} keys; failed for: {', '.join(result['failed'])}.")
    else:
        messages.success(request, f"Reset usage on {result['reset']} keys.")
    return redirect("usage_dashboard")
