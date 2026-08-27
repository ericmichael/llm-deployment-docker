"""CSV import / roster service tests (no LiteLLM calls: revocation is mocked)."""

from io import BytesIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from chat import services
from chat.tests.mocks import ProxyMockMixin
from chat.models import Course, Enrollment

User = get_user_model()


def upload(text, encoding="utf-8"):
    return BytesIO(text.encode(encoding))


class CsvImportTests(ProxyMockMixin, TestCase):
    def setUp(self):
        self.course = Course.objects.create(name="AI", code="CS-1", semester="Fall")

    def test_bom_and_header_case_are_tolerated(self):
        results = services.process_csv_import(
            upload("﻿Email \na@x.edu\nB@X.EDU\n"), self.course, Enrollment.Role.STUDENT
        )
        self.assertEqual(results["errors"], [])
        self.assertEqual(results["created_enrollments"], 2)
        self.assertTrue(User.objects.filter(email="b@x.edu").exists())

    def test_short_row_does_not_crash_or_roll_back_others(self):
        results = services.process_csv_import(
            upload("name,email\nAlice,a@x.edu\nBob\nCarol,c@x.edu\n"), self.course, Enrollment.Role.STUDENT
        )
        self.assertEqual(results["created_enrollments"], 2)
        self.assertEqual(results["skipped"], 1)
        self.assertIn("Row 3", results["errors"][0])

    def test_invalid_email_rejected(self):
        results = services.process_csv_import(
            upload("email\nnot-an-email\n"), self.course, Enrollment.Role.STUDENT
        )
        self.assertEqual(results["created_users"], 0)
        self.assertEqual(len(results["errors"]), 1)

    def test_existing_ta_in_roster_is_reported_not_500(self):
        ta = User.objects.create_user(email="ta@x.edu")
        Enrollment.objects.create(course=self.course, user=ta, role=Enrollment.Role.TA)
        results = services.process_csv_import(
            upload("email\nta@x.edu\nnew@x.edu\n"), self.course, Enrollment.Role.STUDENT
        )
        self.assertEqual(results["created_enrollments"], 1)
        self.assertEqual(results["skipped"], 1)
        self.assertIn("already a TA", results["errors"][0])

    def test_missing_email_column(self):
        results = services.process_csv_import(upload("name\nBob\n"), self.course, Enrollment.Role.STUDENT)
        self.assertIn("must have an 'email' column", results["errors"][0])

    def test_student_moved_between_courses(self):
        other = Course.objects.create(name="Old", code="CS-0", semester="Spring")
        user = User.objects.create_user(email="s@x.edu")
        Enrollment.objects.create(course=other, user=user, role=Enrollment.Role.STUDENT)
        result = services.add_student_to_course(self.course, "S@x.edu")
        self.assertTrue(result["success"])
        self.assertIn("moved from CS-0", result["message"])
        self.assertEqual(Enrollment.objects.filter(user=user).count(), 1)


class EmailNormalizationTests(TestCase):
    def test_create_user_lowercases(self):
        user = User.objects.create_user(email="Mixed@Case.EDU")
        self.assertEqual(user.email, "mixed@case.edu")

    def test_save_lowercases(self):
        user = User(email="Upper@X.edu")
        user.save()
        self.assertEqual(User.objects.get(pk=user.pk).email, "upper@x.edu")
