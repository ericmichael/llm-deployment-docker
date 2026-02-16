"""
Business logic for course and enrollment management.

Extracted from admin.py so it can be reused by both Django admin
and the professor-facing course management views.
"""

import csv
import io

from django.contrib.auth import get_user_model
from django.db import transaction

from .models import Course, Enrollment

User = get_user_model()


def get_or_create_user(email, student_id):
    """
    Get or create a user by email.
    If created, sets password to ai_<student_id>.
    Returns (user, was_created).
    """
    email = email.strip().lower()
    user, created = User.objects.get_or_create(
        email=email,
        defaults={"is_active": True},
    )
    if created:
        user.set_password(f"ai_{student_id}")
        user.save()
    return user, created


def add_student_to_course(course, email, student_id):
    """
    Add a single student to a course.
    Handles user creation, move-from-other-course, and duplicate detection.
    Returns dict with keys: success, message, message_type.
    """
    user, user_created = get_or_create_user(email, student_id)

    if Enrollment.objects.filter(course=course, user=user).exists():
        return {
            "success": False,
            "message": f"{email} is already enrolled in this course.",
            "message_type": "warning",
        }

    moved_from = None
    existing = Enrollment.objects.filter(
        user=user, role=Enrollment.Role.STUDENT
    ).first()
    if existing:
        moved_from = existing.course.code
        existing.delete()

    Enrollment.objects.create(
        course=course,
        user=user,
        student_id=student_id,
        role=Enrollment.Role.STUDENT,
    )

    suffix = f" (moved from {moved_from})" if moved_from else ""
    prefix = "Created user and added" if user_created else "Added"
    return {
        "success": True,
        "message": f"{prefix} {email} as student{suffix}.",
        "message_type": "success",
    }


def add_ta_to_course(course, email, student_id):
    """
    Add a single TA to a course.
    Returns dict with keys: success, message, message_type.
    """
    user, user_created = get_or_create_user(email, student_id)

    if Enrollment.objects.filter(course=course, user=user).exists():
        return {
            "success": False,
            "message": f"{email} is already enrolled in this course.",
            "message_type": "warning",
        }

    Enrollment.objects.create(
        course=course,
        user=user,
        student_id=student_id,
        role=Enrollment.Role.TA,
    )

    prefix = "Created user and added" if user_created else "Added"
    return {
        "success": True,
        "message": f"{prefix} {email} as TA.",
        "message_type": "success",
    }


def process_csv_import(csv_file, course, role):
    """
    Process a CSV file and create users/enrollments.
    csv_file is a Django UploadedFile (has .read()).
    role is an Enrollment.Role value ('student' or 'ta').
    Returns results dict with created_users, created_enrollments,
    moved_enrollments, skipped, errors.
    """
    results = {
        "created_users": 0,
        "created_enrollments": 0,
        "moved_enrollments": 0,
        "skipped": 0,
        "errors": [],
    }

    # Decode and parse CSV
    try:
        decoded = csv_file.read().decode("utf-8")
        reader = csv.DictReader(io.StringIO(decoded))
        rows = list(reader)
    except Exception as e:
        results["errors"].append(f"Error reading CSV: {e}")
        return results

    if not rows:
        results["errors"].append("CSV file is empty")
        return results

    # Validate columns
    required = {"email", "student_id"}
    if not required.issubset(rows[0].keys()):
        results["errors"].append(
            f"CSV must have columns: email, student_id. Found: {', '.join(rows[0].keys())}"
        )
        return results

    is_student = role == Enrollment.Role.STUDENT

    with transaction.atomic():
        for i, row in enumerate(rows, start=2):  # Start at 2 (header is row 1)
            email = row.get("email", "").strip().lower()
            student_id = row.get("student_id", "").strip()

            if not email or not student_id:
                results["errors"].append(f"Row {i}: Missing email or student_id")
                results["skipped"] += 1
                continue

            # Get or create user
            user, user_created = User.objects.get_or_create(
                email=email,
                defaults={"is_active": True},
            )

            if user_created:
                user.set_password(f"ai_{student_id}")
                user.save()
                results["created_users"] += 1

            # Check existing enrollment
            if is_student:
                existing = Enrollment.objects.filter(
                    user=user, role=Enrollment.Role.STUDENT
                ).first()
                if existing:
                    if existing.course == course:
                        results["skipped"] += 1
                        continue
                    else:
                        # Move student to new course
                        existing.delete()
                        results["moved_enrollments"] += 1
            else:
                # TA - check if already in this course
                if Enrollment.objects.filter(course=course, user=user).exists():
                    results["skipped"] += 1
                    continue

            # Create enrollment
            Enrollment.objects.create(
                course=course,
                user=user,
                student_id=student_id,
                role=role,
            )
            results["created_enrollments"] += 1

    return results


def remove_enrollment(enrollment_id):
    """Delete an enrollment by ID. Returns success/message dict."""
    try:
        enrollment = Enrollment.objects.get(pk=enrollment_id)
        email = enrollment.user.email
        enrollment.delete()
        return {"success": True, "message": f"Removed {email} from course."}
    except Enrollment.DoesNotExist:
        return {"success": False, "message": "Enrollment not found."}


def reset_enrollment_password(enrollment_id):
    """Reset a user's password to ai_<student_id>. Returns success/message dict."""
    try:
        enrollment = Enrollment.objects.get(pk=enrollment_id)
        enrollment.user.set_password(f"ai_{enrollment.student_id}")
        enrollment.user.save()
        return {
            "success": True,
            "message": f"Password reset for {enrollment.user.email}.",
        }
    except Enrollment.DoesNotExist:
        return {"success": False, "message": "Enrollment not found."}
