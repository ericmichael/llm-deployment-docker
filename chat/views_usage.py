"""Staff-only usage dashboard views."""

from datetime import datetime

from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import render

from . import services_usage
from .models import Course, Enrollment


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

    logs_result = services_usage.get_spend_logs(start_date, end_date)
    if not logs_result["success"]:
        errors.append(logs_result["message"])

    agg = services_usage.aggregate_from_logs(logs_result["logs"], filter_emails=filter_emails)

    return render(request, "usage/dashboard.html", {
        "total_spend": agg["total_spend"],
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
