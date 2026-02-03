"""
Management command to import TAs from a CSV file into a course.

Usage:
    python manage.py import_tas COURSE_CODE path/to/tas.csv

CSV format:
    email,student_id
    ta@university.edu,12345678

The command will:
    - Create user accounts with password ai_<student_id>
    - Enroll TAs in the specified course
    - TAs can be enrolled in multiple courses (unlike students)
    - Skip TAs already enrolled in this specific course
"""
import csv
from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth import get_user_model
from django.db import transaction

from chat.models import Course, Enrollment


User = get_user_model()


class Command(BaseCommand):
    help = "Import TAs from a CSV file into a course"

    def add_arguments(self, parser):
        parser.add_argument(
            "course_code",
            type=str,
            help="The course code to enroll TAs in (e.g., CSCI-4380-01)",
        )
        parser.add_argument(
            "csv_file",
            type=str,
            help="Path to CSV file with columns: email,student_id",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be done without making changes",
        )

    def handle(self, *args, **options):
        course_code = options["course_code"]
        csv_file = options["csv_file"]
        dry_run = options["dry_run"]

        # Find the course
        try:
            course = Course.objects.get(code=course_code)
        except Course.DoesNotExist:
            raise CommandError(f"Course with code '{course_code}' does not exist")

        self.stdout.write(f"Importing TAs into: {course}")
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN - no changes will be made"))

        # Read CSV file
        try:
            with open(csv_file, "r", newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
        except FileNotFoundError:
            raise CommandError(f"CSV file not found: {csv_file}")
        except Exception as e:
            raise CommandError(f"Error reading CSV file: {e}")

        # Validate CSV has required columns
        if not rows:
            raise CommandError("CSV file is empty")

        required_columns = {"email", "student_id"}
        if not required_columns.issubset(rows[0].keys()):
            raise CommandError(
                f"CSV must have columns: {', '.join(required_columns)}. "
                f"Found: {', '.join(rows[0].keys())}"
            )

        created_users = 0
        created_enrollments = 0
        skipped = 0

        with transaction.atomic():
            for row in rows:
                email = row["email"].strip().lower()
                student_id = row["student_id"].strip()

                if not email or not student_id:
                    self.stdout.write(
                        self.style.WARNING(f"  Skipping row with missing data: {row}")
                    )
                    skipped += 1
                    continue

                # Get or create user
                user, user_created = User.objects.get_or_create(
                    email=email,
                    defaults={"is_active": True},
                )

                if user_created:
                    password = f"ai_{student_id}"
                    if not dry_run:
                        user.set_password(password)
                        user.save()
                        # Create API token
                    created_users += 1
                    self.stdout.write(f"  Created user: {email}")

                # Check if already enrolled in this course
                existing = Enrollment.objects.filter(
                    course=course, user=user
                ).exists()

                if existing:
                    self.stdout.write(
                        f"  Skipping {email}: already enrolled in this course"
                    )
                    skipped += 1
                    continue

                # Create TA enrollment
                if not dry_run:
                    Enrollment.objects.create(
                        course=course,
                        user=user,
                        student_id=student_id,
                        role=Enrollment.Role.TA,
                    )
                created_enrollments += 1
                self.stdout.write(f"  Enrolled TA: {email} (ID: {student_id})")

            if dry_run:
                # Rollback all changes in dry run
                transaction.set_rollback(True)

        # Summary
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Import complete:"))
        self.stdout.write(f"  Users created: {created_users}")
        self.stdout.write(f"  TA enrollments created: {created_enrollments}")
        self.stdout.write(f"  Skipped: {skipped}")

        if dry_run:
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING("This was a dry run. No changes were made.")
            )
