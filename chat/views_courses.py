from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from django.conf import settings

from . import litellm_keys, services, services_models
from .forms import AddStudentForm, AddTAForm, BudgetForm, CourseAccessForm, CourseForm, CSVImportForm
from .models import Course, Enrollment


@staff_member_required
def course_list(request):
    """List all courses with student/TA counts."""
    courses = Course.objects.annotate(
        student_count=Count(
            "enrollments", filter=Q(enrollments__role=Enrollment.Role.STUDENT)
        ),
        ta_count=Count(
            "enrollments", filter=Q(enrollments__role=Enrollment.Role.TA)
        ),
    ).order_by("-created_at")
    form = CourseForm()
    return render(request, "courses/course_list.html", {
        "courses": courses,
        "form": form,
    })


@staff_member_required
@require_POST
def course_create(request):
    """Create a new course."""
    form = CourseForm(request.POST)
    if form.is_valid():
        course = form.save()
        messages.success(request, f"Course {course.code} created.")
        return redirect("course_detail", course_id=course.pk)
    # Re-render list with form errors
    courses = Course.objects.annotate(
        student_count=Count(
            "enrollments", filter=Q(enrollments__role=Enrollment.Role.STUDENT)
        ),
        ta_count=Count(
            "enrollments", filter=Q(enrollments__role=Enrollment.Role.TA)
        ),
    ).order_by("-created_at")
    return render(request, "courses/course_list.html", {
        "courses": courses,
        "form": form,
    })


@staff_member_required
@require_POST
def course_toggle_active(request, course_id):
    """Toggle a course's is_active flag."""
    course = get_object_or_404(Course, pk=course_id)
    course.is_active = not course.is_active
    course.save(update_fields=["is_active"])
    status = "activated" if course.is_active else "deactivated"
    messages.success(request, f"{course.code} {status}.")
    return redirect("course_list")


@staff_member_required
def course_detail(request, course_id):
    """Course detail page showing roster."""
    course = get_object_or_404(Course, pk=course_id)
    students = course.enrollments.filter(
        role=Enrollment.Role.STUDENT
    ).select_related("user").order_by("user__email")
    tas = course.enrollments.filter(
        role=Enrollment.Role.TA
    ).select_related("user").order_by("user__email")

    # Check for CSV import results in session
    csv_results = request.session.pop("csv_import_results", None)

    for enrollment in list(students) + list(tas):
        enrollment.effective_budget, enrollment.budget_source = (
            litellm_keys.effective_budget_with_source(enrollment.user)
        )

    return render(request, "courses/course_detail.html", {
        "course": course,
        "students": students,
        "tas": tas,
        "default_budget": settings.LITELLM_KEY_MAX_BUDGET,
        "budget_form": BudgetForm(initial={"monthly_budget": course.monthly_budget}),
        "access_form": CourseAccessForm(
            initial={"total_budget": course.total_budget, "allowed_models": course.allowed_models},
            model_choices=services_models.model_names(extra=course.allowed_models),
        ),
        "team_usage": litellm_keys.team_usage(course),
        "add_student_form": AddStudentForm(),
        "add_ta_form": AddTAForm(),
        "csv_form": CSVImportForm(),
        "csv_results": csv_results,
    })


@staff_member_required
@require_POST
def course_add_student(request, course_id):
    """Add a single student to a course."""
    course = get_object_or_404(Course, pk=course_id)
    form = AddStudentForm(request.POST)
    if form.is_valid():
        result = services.add_student_to_course(
            course,
            form.cleaned_data["email"],
        )
        getattr(messages, result["message_type"])(request, result["message"])
    else:
        messages.error(request, "Invalid form data.")
    return redirect("course_detail", course_id=course.pk)


@staff_member_required
@require_POST
def course_add_ta(request, course_id):
    """Add a single TA to a course."""
    course = get_object_or_404(Course, pk=course_id)
    form = AddTAForm(request.POST)
    if form.is_valid():
        result = services.add_ta_to_course(
            course,
            form.cleaned_data["email"],
        )
        getattr(messages, result["message_type"])(request, result["message"])
    else:
        messages.error(request, "Invalid form data.")
    return redirect("course_detail", course_id=course.pk)


@staff_member_required
@require_POST
def course_import_csv(request, course_id):
    """Bulk CSV import for a course."""
    course = get_object_or_404(Course, pk=course_id)
    form = CSVImportForm(request.POST, request.FILES)
    if form.is_valid():
        results = services.process_csv_import(
            form.cleaned_data["csv_file"],
            course,
            form.cleaned_data["role"],
        )
        request.session["csv_import_results"] = results
    else:
        messages.error(request, "Please select a CSV file.")
    return redirect("course_detail", course_id=course.pk)


@staff_member_required
@require_POST
def enrollment_remove(request, enrollment_id):
    """Remove an enrollment."""
    enrollment = get_object_or_404(Enrollment, pk=enrollment_id)
    course_id = enrollment.course_id
    result = services.remove_enrollment(enrollment_id)
    msg_level = "success" if result["success"] else "error"
    getattr(messages, msg_level)(request, result["message"])
    return redirect("course_detail", course_id=course_id)


@staff_member_required
@require_POST
def course_set_budget(request, course_id):
    """Set (or clear) the course-level monthly budget; keys re-sync via signal."""
    course = get_object_or_404(Course, pk=course_id)
    form = BudgetForm(request.POST)
    if form.is_valid():
        course.monthly_budget = form.cleaned_data["monthly_budget"]
        course.save(update_fields=["monthly_budget"])
        value = course.monthly_budget
        label = "global default" if value is None else ("unlimited" if value == 0 else f"${value}")
        messages.success(request, f"{course.code} budget set to {label}. Student keys re-synced.")
    else:
        messages.error(request, "Enter a non-negative amount, or leave blank for the default.")
    return redirect("course_detail", course_id=course.pk)


@staff_member_required
@require_POST
def enrollment_set_budget(request, enrollment_id):
    """Set (or clear) a per-user monthly budget override; key re-syncs via signal."""
    enrollment = get_object_or_404(Enrollment.objects.select_related("user"), pk=enrollment_id)
    form = BudgetForm(request.POST)
    if form.is_valid():
        user = enrollment.user
        user.monthly_budget = form.cleaned_data["monthly_budget"]
        user.save(update_fields=["monthly_budget"])
        value = user.monthly_budget
        label = "inherited" if value is None else ("unlimited" if value == 0 else f"${value}")
        messages.success(request, f"Budget for {user.email}: {label}.")
    else:
        messages.error(request, "Enter a non-negative amount, or leave blank to inherit.")
    return redirect("course_detail", course_id=enrollment.course_id)


def _report_reset(request, result, scope):
    if result["failed"]:
        messages.warning(
            request,
            f"Reset usage for {result['reset']} {scope}; failed for: {', '.join(result['failed'])}.",
        )
    elif result["reset"]:
        messages.success(request, f"Reset usage for {result['reset']} {scope}.")
    else:
        messages.info(request, f"No API keys to reset for {scope}.")


@staff_member_required
@require_POST
def enrollment_reset_usage(request, enrollment_id):
    """Zero one student's spend counter so they can keep working this month."""
    enrollment = get_object_or_404(Enrollment.objects.select_related("user"), pk=enrollment_id)
    result = litellm_keys.reset_spend_for_users([enrollment.user])
    _report_reset(request, result, enrollment.user.email)
    return redirect("course_detail", course_id=enrollment.course_id)


@staff_member_required
@require_POST
def course_reset_usage(request, course_id):
    """Zero the spend counter of every student and TA in the course."""
    course = get_object_or_404(Course, pk=course_id)
    users = [e.user for e in course.enrollments.select_related("user")]
    result = litellm_keys.reset_spend_for_users(users)
    _report_reset(request, result, f"members of {course.code}")
    return redirect("course_detail", course_id=course.pk)


@staff_member_required
@require_POST
def course_set_access(request, course_id):
    """Set the course-wide monthly cap and model allowlist (synced to the LiteLLM team + member keys)."""
    course = get_object_or_404(Course, pk=course_id)
    choices = services_models.model_names(extra=course.allowed_models)
    form = CourseAccessForm(request.POST, model_choices=choices)
    if form.is_valid():
        course.total_budget = form.cleaned_data["total_budget"]
        course.allowed_models = sorted(form.cleaned_data["allowed_models"])
        course.save(update_fields=["total_budget", "allowed_models"])
        models_label = ", ".join(course.allowed_models) if course.allowed_models else "all models"
        cap_label = f"${course.total_budget}/month" if course.total_budget else "no course-wide cap"
        messages.success(request, f"{course.code}: {cap_label}; models: {models_label}. Member keys re-synced.")
    else:
        messages.error(request, "Invalid access settings.")
    return redirect("course_detail", course_id=course.pk)
