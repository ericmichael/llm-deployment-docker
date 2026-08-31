from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from unittest import mock

from chat.models import Course, Enrollment
from chat.tests.mocks import ProxyMockMixin

User = get_user_model()


class CourseCreateTests(ProxyMockMixin, TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(email="prof@x.edu", password="pw", is_staff=True)
        self.client.force_login(self.staff)

    def test_created_course_is_active(self):
        resp = self.client.post(
            reverse("course_create"), {"code": "CS-9", "name": "New", "semester": "Fall"}
        )
        course = Course.objects.get(code="CS-9")
        self.assertRedirects(resp, reverse("course_detail", args=[course.pk]))
        self.assertTrue(course.is_active)

    def test_non_staff_cannot_create(self):
        student = User.objects.create_user(email="s@x.edu", password="pw")
        self.client.force_login(student)
        resp = self.client.post(
            reverse("course_create"), {"code": "CS-9", "name": "New", "semester": "Fall"}
        )
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Course.objects.filter(code="CS-9").exists())


class EnrollmentTableEscapingTests(ProxyMockMixin, TestCase):
    def test_email_is_js_escaped_in_confirm(self):
        staff = User.objects.create_user(email="prof@x.edu", password="pw", is_staff=True)
        self.client.force_login(staff)
        course = Course.objects.create(name="AI", code="CS-1", semester="Fall")
        # Bypass validation on purpose to simulate a tampered roster
        evil = User(email="x')+alert(1)+('@u.edu")
        User.objects.bulk_create([evil])
        course.enrollments.create(user=evil, role="student")
        resp = self.client.get(reverse("course_detail", args=[course.pk]))
        self.assertNotContains(resp, "confirm('Remove x')+alert(1)")
        self.assertContains(resp, "\\u0027")


class BudgetViewTests(ProxyMockMixin, TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(email="prof@x.edu", password="pw", is_staff=True)
        self.client.force_login(self.staff)
        self.course = Course.objects.create(name="AI", code="CS-1", semester="Fall")
        self.student = User.objects.create_user(email="s@x.edu")
        self.enrollment = Enrollment.objects.create(course=self.course, user=self.student, role="student")

    def test_set_and_clear_course_budget(self):
        self.client.post(reverse("course_set_budget", args=[self.course.pk]), {"monthly_budget": "12.50"})
        self.course.refresh_from_db()
        self.assertEqual(str(self.course.monthly_budget), "12.50")
        self.client.post(reverse("course_set_budget", args=[self.course.pk]), {"monthly_budget": ""})
        self.course.refresh_from_db()
        self.assertIsNone(self.course.monthly_budget)

    def test_negative_budget_rejected(self):
        resp = self.client.post(reverse("course_set_budget", args=[self.course.pk]), {"monthly_budget": "-1"}, follow=True)
        self.course.refresh_from_db()
        self.assertIsNone(self.course.monthly_budget)
        self.assertContains(resp, "non-negative")

    def test_set_student_override(self):
        self.client.post(reverse("enrollment_set_budget", args=[self.enrollment.pk]), {"monthly_budget": "2"})
        self.student.refresh_from_db()
        self.assertEqual(float(self.student.monthly_budget), 2.0)

    def test_detail_page_shows_effective_budget(self):
        self.course.monthly_budget = 30
        self.course.save()
        resp = self.client.get(reverse("course_detail", args=[self.course.pk]))
        self.assertContains(resp, "$30.00 (course CS-1)")

    def test_create_course_with_budget(self):
        self.client.post(reverse("course_create"), {"code": "CS-9", "name": "N", "semester": "F", "monthly_budget": "5"})
        self.assertEqual(float(Course.objects.get(code="CS-9").monthly_budget), 5.0)


class ResetUsageViewTests(ProxyMockMixin, TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(email="prof@x.edu", password="pw", is_staff=True)
        self.client.force_login(self.staff)
        self.course = Course.objects.create(name="AI", code="CS-1", semester="Fall")
        self.student = User.objects.create_user(email="s@x.edu", litellm_key="sk-s")
        self.enrollment = Enrollment.objects.create(course=self.course, user=self.student, role="student")
        other = User.objects.create_user(email="o@x.edu", litellm_key="sk-o")  # not in the course
        patcher = mock.patch("chat.litellm_keys.reset_spend", return_value=True)
        self.reset = patcher.start()
        self.addCleanup(patcher.stop)

    def test_reset_one_student(self):
        resp = self.client.post(reverse("enrollment_reset_usage", args=[self.enrollment.pk]), follow=True)
        self.reset.assert_called_once()
        self.assertEqual(self.reset.call_args[0][0].pk, self.student.pk)
        self.assertContains(resp, "Reset usage for 1")

    def test_reset_course_only_touches_members(self):
        self.client.post(reverse("course_reset_usage", args=[self.course.pk]))
        emails = {c.args[0].email for c in self.reset.call_args_list}
        self.assertEqual(emails, {"s@x.edu"})

    def test_reset_all(self):
        self.client.post(reverse("usage_reset_all"))
        emails = {c.args[0].email for c in self.reset.call_args_list}
        self.assertEqual(emails, {"s@x.edu", "o@x.edu"})

    def test_get_not_allowed(self):
        self.assertEqual(self.client.get(reverse("usage_reset_all")).status_code, 405)

    def test_non_staff_forbidden(self):
        self.client.force_login(self.student)
        self.client.post(reverse("usage_reset_all"))
        self.reset.assert_not_called()


class CourseAccessViewTests(ProxyMockMixin, TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(email="prof@x.edu", password="pw", is_staff=True)
        self.client.force_login(self.staff)
        self.course = Course.objects.create(name="AI", code="CS-1", semester="Fall")

    @mock.patch("chat.services_models.model_names", return_value=["gpt-4o", "gpt-4o-mini", "gpt-5"])
    def test_set_cap_and_models(self, _names):
        resp = self.client.post(
            reverse("course_set_access", args=[self.course.pk]),
            {"total_budget": "250", "allowed_models": ["gpt-4o-mini", "gpt-5"]},
            follow=True,
        )
        self.course.refresh_from_db()
        self.assertEqual(float(self.course.total_budget), 250.0)
        self.assertEqual(self.course.allowed_models, ["gpt-4o-mini", "gpt-5"])
        self.assertContains(resp, "$250/month")

    @mock.patch("chat.services_models.model_names", return_value=["gpt-4o"])
    def test_detail_renders_access_form(self, _names):
        resp = self.client.get(reverse("course_detail", args=[self.course.pk]))
        self.assertContains(resp, "Allowed models")
        self.assertContains(resp, 'value="gpt-4o"')


class RegenerateKeyViewTests(ProxyMockMixin, TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="s@x.edu", password="pw", litellm_key="sk-old")
        course = Course.objects.create(name="AI", code="CS-1", semester="Fall")
        Enrollment.objects.create(course=course, user=self.user, role="student")
        self.client.force_login(self.user)

    @mock.patch("chat.litellm_keys.regenerate_key", return_value="sk-new")
    @override_settings(LITELLM_ENABLE_VIRTUAL_KEYS=True)
    def test_enrolled_user_can_regenerate(self, regen):
        resp = self.client.post(reverse("regenerate_key"), follow=True)
        regen.assert_called_once()
        self.assertContains(resp, "regenerated")

    @mock.patch("chat.litellm_keys.regenerate_key")
    @override_settings(LITELLM_ENABLE_VIRTUAL_KEYS=True)
    def test_unenrolled_user_cannot(self, regen):
        Enrollment.objects.all().delete()
        self.client.post(reverse("regenerate_key"))
        regen.assert_not_called()

    def test_get_not_allowed(self):
        self.assertEqual(self.client.get(reverse("regenerate_key")).status_code, 405)


class RevokeKeysViewTests(ProxyMockMixin, TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(email="prof@x.edu", password="pw", is_staff=True)
        self.client.force_login(self.staff)
        self.course = Course.objects.create(name="AI", code="CS-1", semester="Fall", is_active=False)
        self.student = User.objects.create_user(email="s@x.edu", litellm_key="sk-s")
        Enrollment.objects.create(course=self.course, user=self.student, role="student")
        self.other = Course.objects.create(name="B", code="CS-2", semester="Fall")
        self.ta = User.objects.create_user(email="t@x.edu", litellm_key="sk-t")
        Enrollment.objects.create(course=self.course, user=self.ta, role="ta")
        Enrollment.objects.create(course=self.other, user=self.ta, role="student")

    def test_revokes_only_the_unentitled(self):
        resp = self.client.post(reverse("course_revoke_keys", args=[self.course.pk]), follow=True)
        revoked = {c.args[0].email for c in self.proxy["revoke_key"].call_args_list}
        self.assertEqual(revoked, {"s@x.edu"})   # ta is still in an active course
        self.assertContains(resp, "Revoked 1 key")
        self.assertContains(resp, "Kept 1 key")

    def test_non_staff_forbidden(self):
        self.client.force_login(self.student)
        self.client.post(reverse("course_revoke_keys", args=[self.course.pk]))
        self.proxy["revoke_key"].assert_not_called()

    def test_get_not_allowed(self):
        self.assertEqual(self.client.get(reverse("course_revoke_keys", args=[self.course.pk])).status_code, 405)
