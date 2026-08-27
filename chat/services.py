"""
Business logic for course and enrollment management.

Extracted from admin.py so it can be reused by both Django admin
and the professor-facing course management views.
"""

import csv
import io

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction

from . import litellm_keys
from .models import Course, Enrollment

User = get_user_model()

EMAIL_MAX_LENGTH = User._meta.get_field("email").max_length


def normalize_email(email):
    """Lowercase/trim an email and validate it. Returns the clean value or raises ValidationError."""
    email = (email or "").strip().lower()
    if not email:
        raise ValidationError("Missing email")
    if len(email) > EMAIL_MAX_LENGTH:
        raise ValidationError("Email is too long")
    validate_email(email)
    return email


def get_or_create_user(email):
    """
    Get or create a user by email.
    New users get an unusable password (SSO handles authentication).
    Returns (user, was_created).
    """
    email = normalize_email(email)
    user, created = User.objects.get_or_create(
        email=email,
        defaults={"is_active": True},
    )
    if created:
        user.set_unusable_password()
        user.save()
    return user, created


def _enroll_student(course, user):
    """
    Enroll `user` as a student in `course`, moving them from any other
    course. Returns (outcome, moved_from_code) where outcome is one of
    "created", "already", "is_ta".
    Must be called inside a transaction.
    """
    if Enrollment.objects.filter(course=course, user=user).exists():
        existing_role = Enrollment.objects.get(course=course, user=user).role
        return ("already" if existing_role == Enrollment.Role.STUDENT else "is_ta"), None

    moved_from = None
    existing = (
        Enrollment.objects.select_for_update(of=("self",))
        .filter(user=user, role=Enrollment.Role.STUDENT)
        .select_related("course")
        .first()
    )
    if existing:
        moved_from = existing.course
        existing._moving = True  # tell the post_delete signal not to revoke the key
        existing.delete()

    Enrollment.objects.create(course=course, user=user, role=Enrollment.Role.STUDENT)
    return "created", moved_from


def add_student_to_course(course, email):
    """
    Add a single student to a course.
    Handles user creation, move-from-other-course, and duplicate detection.
    Returns dict with keys: success, message, message_type.
    """
    try:
        user, user_created = get_or_create_user(email)
    except ValidationError as exc:
        return {"success": False, "message": f"{email}: {exc.messages[0]}", "message_type": "error"}
    email = user.email

    try:
        with transaction.atomic():
            outcome, moved_from = _enroll_student(course, user)
    except IntegrityError:
        return {
            "success": False,
            "message": f"{email} could not be enrolled (concurrent change). Please retry.",
            "message_type": "error",
        }

    if outcome == "already":
        return {"success": False, "message": f"{email} is already enrolled in this course.", "message_type": "warning"}
    if outcome == "is_ta":
        return {"success": False, "message": f"{email} is already a TA in this course.", "message_type": "warning"}

    suffix = f" (moved from {moved_from.code})" if moved_from else ""
    prefix = "Created user and added" if user_created else "Added"
    return {"success": True, "message": f"{prefix} {email} as student{suffix}.", "message_type": "success"}


def add_ta_to_course(course, email):
    """
    Add a single TA to a course.
    Returns dict with keys: success, message, message_type.
    """
    try:
        user, user_created = get_or_create_user(email)
    except ValidationError as exc:
        return {"success": False, "message": f"{email}: {exc.messages[0]}", "message_type": "error"}
    email = user.email

    if Enrollment.objects.filter(course=course, user=user).exists():
        return {"success": False, "message": f"{email} is already enrolled in this course.", "message_type": "warning"}

    try:
        Enrollment.objects.create(course=course, user=user, role=Enrollment.Role.TA)
    except IntegrityError:
        return {"success": False, "message": f"{email} is already enrolled in this course.", "message_type": "warning"}

    prefix = "Created user and added" if user_created else "Added"
    return {"success": True, "message": f"{prefix} {email} as TA.", "message_type": "success"}


def read_csv_rows(raw_bytes):
    """
    Parse CSV bytes into a list of dicts with normalized (lowercased,
    stripped) header names. Tolerates a UTF-8 BOM (Excel) and short rows.
    Raises ValueError with a user-facing message on structural problems.
    """
    try:
        decoded = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("CSV must be UTF-8 encoded")

    reader = csv.DictReader(io.StringIO(decoded))
    fieldnames = [(name or "").strip().lower() for name in (reader.fieldnames or [])]
    if not fieldnames:
        raise ValueError("CSV file is empty")
    if "email" not in fieldnames:
        raise ValueError(f"CSV must have an 'email' column. Found: {', '.join(fieldnames)}")

    rows = []
    for raw in reader:
        row = {}
        for key, value in raw.items():
            if key is None:  # extra unnamed columns
                continue
            row[key.strip().lower()] = (value or "").strip()
        rows.append(row)
    return rows


def process_csv_import(csv_file, course, role):
    """
    Process a CSV file and create users/enrollments.
    csv_file is a Django UploadedFile (has .read()).
    role is an Enrollment.Role value ('student' or 'ta').
    CSV must have an 'email' column (case-insensitive header).
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

    try:
        rows = read_csv_rows(csv_file.read())
    except ValueError as e:
        results["errors"].append(str(e))
        return results
    except Exception as e:
        results["errors"].append(f"Error reading CSV: {e}")
        return results

    if not rows:
        results["errors"].append("CSV file has no data rows")
        return results

    is_student = role == Enrollment.Role.STUDENT

    for i, row in enumerate(rows, start=2):  # Start at 2 (header is row 1)
        try:
            email = normalize_email(row.get("email", ""))
        except ValidationError as exc:
            results["errors"].append(f"Row {i}: {exc.messages[0]}")
            results["skipped"] += 1
            continue

        # One savepoint per row so a bad row doesn't roll back the good ones.
        try:
            with transaction.atomic():
                user, user_created = get_or_create_user(email)
                if user_created:
                    results["created_users"] += 1

                if is_student:
                    outcome, moved_from = _enroll_student(course, user)
                    if outcome == "already":
                        results["skipped"] += 1
                        continue
                    if outcome == "is_ta":
                        results["errors"].append(f"Row {i}: {email} is already a TA in this course")
                        results["skipped"] += 1
                        continue
                    if moved_from is not None:
                        results["moved_enrollments"] += 1
                else:
                    if Enrollment.objects.filter(course=course, user=user).exists():
                        results["skipped"] += 1
                        continue
                    Enrollment.objects.create(course=course, user=user, role=role)
                results["created_enrollments"] += 1
        except IntegrityError:
            results["errors"].append(f"Row {i}: {email} could not be enrolled (conflicting enrollment)")
            results["skipped"] += 1

    return results


def remove_enrollment(enrollment_id):
    """Delete an enrollment by ID. Returns success/message dict.

    If the user is left with no active enrollment, their LiteLLM virtual
    key is revoked (via the Enrollment post_delete signal).
    """
    try:
        enrollment = Enrollment.objects.get(pk=enrollment_id)
        user = enrollment.user
        email = user.email
        had_key = bool(user.litellm_key)
        enrollment.delete()

        message = f"Removed {email} from course."
        if had_key and not litellm_keys.user_keeps_key(user):
            message += " Their API key was revoked."
        return {"success": True, "message": message}
    except Enrollment.DoesNotExist:
        return {"success": False, "message": "Enrollment not found."}


def set_courses_active(queryset, is_active):
    """
    Activate/deactivate courses one by one so the Course save signal can
    revoke keys of students left without an active course.
    Returns the number of courses changed.
    """
    changed = 0
    for course in queryset:
        if course.is_active == is_active:
            continue
        course.is_active = is_active
        course.save(update_fields=["is_active"])
        changed += 1
    return changed
