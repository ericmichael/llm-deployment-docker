from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from . import services
from .forms import AddStudentForm, AddTAForm, CourseForm, CSVImportForm
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

    return render(request, "courses/course_detail.html", {
        "course": course,
        "students": students,
        "tas": tas,
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
            form.cleaned_data["student_id"],
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
            form.cleaned_data["student_id"],
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
def enrollment_reset_password(request, enrollment_id):
    """Reset password for an enrolled user."""
    enrollment = get_object_or_404(Enrollment, pk=enrollment_id)
    course_id = enrollment.course_id
    result = services.reset_enrollment_password(enrollment_id)
    msg_level = "success" if result["success"] else "error"
    getattr(messages, msg_level)(request, result["message"])
    return redirect("course_detail", course_id=course_id)
