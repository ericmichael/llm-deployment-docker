"""Virtual key lifecycle: provisioning, expiry renewal, and revocation triggers."""

from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from chat import litellm_keys, services
from chat.tests.mocks import ProxyMockMixin
from chat.models import Course, Enrollment

User = get_user_model()


def fake_generate(user):
    return "sk-new", "id-new", timezone.now() + timedelta(days=180)


class EnsureKeyTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="u@x.edu")

    @mock.patch("chat.litellm_keys.generate_key", side_effect=fake_generate)
    def test_generates_when_missing(self, gen):
        key = litellm_keys.ensure_key(self.user)
        self.assertEqual(key, "sk-new")
        self.user.refresh_from_db()
        self.assertEqual(self.user.litellm_key_id, "id-new")
        self.assertIsNotNone(self.user.litellm_key_expires)
        gen.assert_called_once()

    @mock.patch("chat.litellm_keys.key_info")
    @mock.patch("chat.litellm_keys.generate_key", side_effect=fake_generate)
    def test_reuses_unexpired_key(self, gen, info):
        info.return_value = {"expires": (timezone.now() + timedelta(days=1)).isoformat()}
        self.user.litellm_key = "sk-old"
        self.user.save()
        self.assertEqual(litellm_keys.ensure_key(self.user), "sk-old")
        gen.assert_not_called()

    @mock.patch("chat.litellm_keys.key_info")
    @mock.patch("chat.litellm_keys.generate_key", side_effect=fake_generate)
    def test_renews_expired_key(self, gen, info):
        info.return_value = {"expires": (timezone.now() - timedelta(seconds=1)).isoformat()}
        self.user.litellm_key = "sk-old"
        self.user.save()
        self.assertEqual(litellm_keys.ensure_key(self.user), "sk-new")
        gen.assert_called_once()

    @mock.patch("chat.litellm_keys.key_info", return_value=None)
    @mock.patch("chat.litellm_keys.generate_key", side_effect=fake_generate)
    def test_key_deleted_out_of_band_is_replaced(self, gen, info):
        self.user.litellm_key = "sk-old"
        self.user.litellm_key_expires = timezone.now() + timedelta(days=30)  # local state says valid
        self.user.save()
        self.assertEqual(litellm_keys.ensure_key(self.user), "sk-new")

    @mock.patch("chat.litellm_keys.key_info", return_value=None)
    @mock.patch("chat.litellm_keys.generate_key", side_effect=fake_generate)
    def test_legacy_key_missing_at_proxy_is_regenerated(self, gen, info):
        self.user.litellm_key = "sk-legacy"
        self.user.save()
        self.assertEqual(litellm_keys.ensure_key(self.user), "sk-new")
        info.assert_called_once_with("sk-legacy")

    @mock.patch("chat.litellm_keys.key_info", return_value={"expires": None})
    @mock.patch("chat.litellm_keys.generate_key", side_effect=fake_generate)
    def test_legacy_key_without_expiry_is_kept(self, gen, info):
        self.user.litellm_key = "sk-legacy"
        self.user.save()
        self.assertEqual(litellm_keys.ensure_key(self.user), "sk-legacy")
        gen.assert_not_called()


class RevocationTriggerTests(ProxyMockMixin, TestCase):
    def setUp(self):
        self.course = Course.objects.create(name="AI", code="CS-1", semester="Fall")
        self.user = User.objects.create_user(email="s@x.edu", litellm_key="sk-1")
        self.enrollment = Enrollment.objects.create(
            course=self.course, user=self.user, role=Enrollment.Role.STUDENT
        )

    def test_admin_style_queryset_delete_revokes(self):
        revoke = self.proxy["revoke_key"]
        Enrollment.objects.filter(pk=self.enrollment.pk).delete()
        revoke.assert_called_once()

    def test_course_delete_cascade_revokes(self):
        revoke = self.proxy["revoke_key"]
        self.course.delete()
        revoke.assert_called_once()

    def test_course_deactivation_revokes(self):
        revoke = self.proxy["revoke_key"]
        services.set_courses_active(Course.objects.filter(pk=self.course.pk), False)
        revoke.assert_called_once()

    def test_reactivation_does_not_revoke(self):
        revoke = self.proxy["revoke_key"]
        self.course.is_active = False
        self.course.save()
        revoke.reset_mock()
        self.course.is_active = True
        self.course.save()
        revoke.assert_not_called()

    def test_staff_keep_keys(self):
        revoke = self.proxy["revoke_key"]
        self.user.is_staff = True
        self.user.save()
        self.enrollment.delete()
        revoke.assert_not_called()

    def test_user_with_other_active_course_keeps_key(self):
        revoke = self.proxy["revoke_key"]
        other = Course.objects.create(name="B", code="CS-2", semester="Fall")
        Enrollment.objects.create(course=other, user=self.user, role=Enrollment.Role.TA)
        self.enrollment.delete()
        revoke.assert_not_called()


class SettingsPageTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="s@x.edu", password="pw")
        self.client.force_login(self.user)

    @override_settings(LITELLM_ENABLE_VIRTUAL_KEYS=False)
    def test_disabled_keys_render_empty_not_none(self):
        self.user.is_staff = True
        self.user.save()
        resp = self.client.get(reverse("settings"))
        self.assertNotContains(resp, 'value="None"')

    def test_unenrolled_student_gets_message_and_no_key(self):
        with mock.patch("chat.litellm_keys.ensure_key") as ensure:
            resp = self.client.get(reverse("settings"))
        ensure.assert_not_called()
        self.assertContains(resp, "not enrolled in any active course")

    @override_settings(LITELLM_ENABLE_VIRTUAL_KEYS=True)
    def test_proxy_error_is_not_echoed(self):
        self.user.is_staff = True
        self.user.save()
        with mock.patch("chat.litellm_keys.ensure_key", side_effect=RuntimeError("secret internal url")):
            resp = self.client.get(reverse("settings"))
        self.assertNotContains(resp, "secret internal url")
        self.assertContains(resp, "Could not provision")


@override_settings(LITELLM_KEY_MAX_BUDGET=10.0)
class EffectiveBudgetTests(ProxyMockMixin, TestCase):
    def setUp(self):
        self.sync = self.proxy["sync_key"]
        self.course = Course.objects.create(name="AI", code="CS-1", semester="Fall")
        self.user = User.objects.create_user(email="s@x.edu", litellm_key="sk-1")
        Enrollment.objects.create(course=self.course, user=self.user, role=Enrollment.Role.STUDENT)

    def test_global_default(self):
        self.assertEqual(litellm_keys.effective_budget_with_source(self.user), (10.0, "default"))

    def test_course_overrides_default(self):
        self.course.monthly_budget = 25
        self.course.save()
        self.assertEqual(litellm_keys.effective_budget_with_source(self.user), (25.0, "course CS-1"))

    def test_user_overrides_course(self):
        self.course.monthly_budget = 25
        self.course.save()
        self.user.monthly_budget = 3
        self.user.save()
        self.assertEqual(litellm_keys.effective_budget_with_source(self.user), (3.0, "user"))

    def test_zero_means_unlimited_payload(self):
        self.user.monthly_budget = 0
        self.user.save()
        payload = litellm_keys.key_limit_payload(self.user)
        self.assertIsNone(payload["max_budget"])
        self.assertNotIn("budget_duration", payload)

    def test_inactive_course_budget_ignored(self):
        self.course.monthly_budget = 25
        self.course.is_active = False
        self.course.save()
        self.assertEqual(litellm_keys.effective_budget(self.user), 10.0)

    def test_course_budget_change_resyncs_student_keys(self):
        self.sync.reset_mock()
        self.course.monthly_budget = 25
        self.course.save()
        self.sync.assert_called_once()
        self.assertEqual(self.sync.call_args[0][0].pk, self.user.pk)

    def test_user_budget_change_resyncs(self):
        self.sync.reset_mock()
        self.user.monthly_budget = 1
        self.user.save()
        self.sync.assert_called_once()

    def test_unrelated_user_save_does_not_resync(self):
        self.sync.reset_mock()
        self.user.last_login = timezone.now()
        self.user.save()
        self.sync.assert_not_called()

    def test_moving_courses_resyncs_without_revoking(self):
        revoke = self.proxy["revoke_key"]
        other = Course.objects.create(name="B", code="CS-2", semester="Fall", monthly_budget=50)
        self.sync.reset_mock()
        services.add_student_to_course(other, self.user.email)
        self.assertTrue(self.sync.called)
        revoke.assert_not_called()
        self.user.refresh_from_db()
        self.assertEqual(self.user.litellm_key, "sk-1")
        self.assertEqual(litellm_keys.effective_budget(self.user), 50.0)


class SpendResetTests(ProxyMockMixin, TestCase):
    def setUp(self):
        self.course = Course.objects.create(name="AI", code="CS-1", semester="Fall")
        self.with_key = User.objects.create_user(email="a@x.edu", litellm_key="sk-a")
        self.without_key = User.objects.create_user(email="b@x.edu")
        for u in (self.with_key, self.without_key):
            Enrollment.objects.create(course=self.course, user=u, role=Enrollment.Role.STUDENT)

    @mock.patch("chat.litellm_keys.httpx.Client")
    @override_settings(LITELLM_MASTER_KEY="m")
    def test_reset_spend_posts_zero(self, client_cls):
        client = client_cls.return_value.__enter__.return_value
        client.post.return_value = mock.Mock(status_code=200)
        self.assertTrue(litellm_keys.reset_spend(self.with_key))
        body = client.post.call_args.kwargs["json"]
        self.assertEqual(body, {"key": "sk-a", "spend": 0})
        self.assertFalse(litellm_keys.reset_spend(self.without_key))

    @mock.patch("chat.litellm_keys.reset_spend")
    def test_bulk_reset_reports_failures(self, reset):
        reset.side_effect = [RuntimeError("boom"), False]
        result = litellm_keys.reset_spend_for_users([self.with_key, self.without_key])
        self.assertEqual(result, {"reset": 0, "failed": ["a@x.edu"]})


class CalendarMonthTests(TestCase):
    @override_settings(LITELLM_KEY_MAX_BUDGET=10.0, LITELLM_KEY_BUDGET_DURATION="1mo")
    def test_payload_uses_month_duration(self):
        user = User.objects.create_user(email="c@x.edu")
        payload = litellm_keys.key_limit_payload(user)
        self.assertEqual(payload["budget_duration"], "1mo")


@override_settings(LITELLM_KEY_DEFAULT_MODELS=[])
class TeamDeleteClearsKeysTests(ProxyMockMixin, TestCase):
    def setUp(self):
        self.a = Course.objects.create(name="A", code="CS-A", semester="Fall")
        self.b = Course.objects.create(name="B", code="CS-B", semester="Fall")
        Course.objects.filter(pk=self.a.pk).update(litellm_team_id="team-a")
        Course.objects.filter(pk=self.b.pk).update(litellm_team_id="team-b")
        self.a.refresh_from_db(); self.b.refresh_from_db()
        # keyed to A (student there)
        self.student = User.objects.create_user(email="s@x.edu", litellm_key="sk-s")
        Enrollment.objects.create(course=self.a, user=self.student, role=Enrollment.Role.STUDENT)
        # TA in A but student in B -> key lives in team-b
        self.ta = User.objects.create_user(email="t@x.edu", litellm_key="sk-t")
        Enrollment.objects.create(course=self.a, user=self.ta, role=Enrollment.Role.TA)
        Enrollment.objects.create(course=self.b, user=self.ta, role=Enrollment.Role.STUDENT)
        self.outsider = User.objects.create_user(email="o@x.edu", litellm_key="sk-o")

    def test_only_keys_in_this_team_are_forgotten(self):
        self.assertEqual({u.pk for u in litellm_keys.members_keyed_to(self.a)}, {self.student.pk})
        self.a.delete()
        self.proxy["delete_team_at_proxy"].assert_called_once_with("team-a")
        for u in (self.student, self.ta, self.outsider):
            u.refresh_from_db()
        self.assertEqual(self.student.litellm_key, "")
        self.assertEqual(self.ta.litellm_key, "sk-t")  # untouched: lives in team-b
        self.assertEqual(self.outsider.litellm_key, "sk-o")


class TeamAndModelScopeTests(ProxyMockMixin, TestCase):
    def setUp(self):
        self.course = Course.objects.create(name="AI", code="CS-1", semester="Fall", allowed_models=["gpt-4o-mini"])
        Course.objects.filter(pk=self.course.pk).update(litellm_team_id="team-1")
        self.course.refresh_from_db()
        self.user = User.objects.create_user(email="s@x.edu", litellm_key="sk-1")
        Enrollment.objects.create(course=self.course, user=self.user, role=Enrollment.Role.STUDENT)

    def test_course_create_ensures_team(self):
        self.proxy["ensure_team"].assert_called()

    def test_key_scope_uses_course_team_and_models(self):
        self.assertEqual(litellm_keys.key_scope_payload(self.user), {"models": ["gpt-4o-mini"], "team_id": "team-1"})

    def test_unenrolled_staff_have_no_team_and_all_models(self):
        staff = User.objects.create_user(email="p@x.edu", is_staff=True)
        self.assertEqual(litellm_keys.key_scope_payload(staff), {"models": []})

    @override_settings(LITELLM_KEY_DEFAULT_MODELS=["gpt-4o"])
    def test_default_models_apply_without_course_allowlist(self):
        self.course.allowed_models = []
        self.course.save()
        self.assertEqual(litellm_keys.models_for(self.user), ["gpt-4o"])

    def test_changing_allowed_models_resyncs_team_and_keys(self):
        self.proxy["ensure_team"].reset_mock(); self.proxy["sync_key"].reset_mock()
        self.course.allowed_models = ["gpt-5"]
        self.course.save()
        self.proxy["ensure_team"].assert_called_once()
        self.proxy["sync_key"].assert_called_once()

    def test_total_budget_change_updates_team_only(self):
        self.proxy["ensure_team"].reset_mock(); self.proxy["sync_key"].reset_mock()
        self.course.total_budget = 500
        self.course.save()
        self.proxy["ensure_team"].assert_called_once()
        self.proxy["sync_key"].assert_not_called()

    def test_course_delete_removes_team(self):
        self.course.delete()
        self.proxy["delete_team_at_proxy"].assert_called_once_with("team-1")

    def test_budget_source_matches_key_course(self):
        older = Course.objects.create(name="Old", code="CS-0", semester="Fall")  # no budget, created first
        budgeted = Course.objects.create(name="B", code="CS-2", semester="Fall", monthly_budget=5)
        ta = User.objects.create_user(email="ta2@x.edu")
        Enrollment.objects.create(course=older, user=ta, role=Enrollment.Role.TA)
        Enrollment.objects.create(course=budgeted, user=ta, role=Enrollment.Role.TA)
        self.assertEqual(litellm_keys.primary_course(ta).pk, older.pk)
        # key's course has no budget -> falls back to the budgeted course
        self.assertEqual(litellm_keys.effective_budget_with_source(ta), (5.0, "course CS-2"))
        older.monthly_budget = 7
        older.save()
        self.assertEqual(litellm_keys.effective_budget_with_source(ta), (7.0, "course CS-0"))  # key's course wins

    def test_ta_tiebreak_is_deterministic(self):
        second = Course.objects.create(name="B", code="CS-2", semester="Fall")
        ta = User.objects.create_user(email="ta@x.edu")
        Enrollment.objects.create(course=second, user=ta, role=Enrollment.Role.TA)
        Enrollment.objects.create(course=self.course, user=ta, role=Enrollment.Role.TA)
        for _ in range(3):
            self.assertEqual(litellm_keys.primary_course(ta).pk, self.course.pk)

    def test_deactivation_rescopes_surviving_keys(self):
        other = Course.objects.create(name="B", code="CS-2", semester="Fall")
        Enrollment.objects.create(course=other, user=self.user, role=Enrollment.Role.TA)
        self.proxy["sync_key"].reset_mock()
        self.course.is_active = False
        self.course.save()
        self.proxy["revoke_key"].assert_not_called()  # still enrolled in CS-2
        self.proxy["sync_key"].assert_called()

    @override_settings(LITELLM_KEY_DURATION="180d")
    def test_duration_only_on_generate(self):
        self.assertNotIn("duration", litellm_keys.key_limit_payload(self.user))
        self.assertEqual(litellm_keys.key_expiry_payload(), {"duration": "180d"})

    def test_team_payload(self):
        self.course.total_budget = 500
        with override_settings(LITELLM_KEY_BUDGET_DURATION="1mo"):
            payload = litellm_keys.team_payload(self.course)
        self.assertEqual(payload["team_alias"], "CS-1")
        self.assertEqual(payload["models"], ["gpt-4o-mini"])
        self.assertEqual(payload["max_budget"], 500.0)
        self.assertEqual(payload["budget_duration"], "1mo")


class RegenerateKeyTests(ProxyMockMixin, TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="s@x.edu", litellm_key="sk-old", litellm_key_id="id-old")
        patcher = mock.patch("chat.litellm_keys.delete_key_strict")
        self.delete = patcher.start()
        self.addCleanup(patcher.stop)

    @mock.patch("chat.litellm_keys.generate_key", side_effect=RuntimeError("proxy down"))
    @mock.patch("chat.litellm_keys.key_info", return_value={"spend": 1.0})
    def test_failed_reissue_does_not_keep_dead_key(self, info, gen):
        with self.assertRaises(RuntimeError):
            litellm_keys.regenerate_key(self.user)
        self.user.refresh_from_db()
        self.assertEqual(self.user.litellm_key, "")

    @mock.patch("chat.litellm_keys.generate_key", return_value=("sk-new", "id-new", None))
    @mock.patch("chat.litellm_keys.key_info", return_value={"spend": 4.25})
    def test_rotation_carries_spend_and_revokes_old(self, info, gen):
        self.assertEqual(litellm_keys.regenerate_key(self.user), "sk-new")
        self.delete.assert_called_once_with("sk-old")
        self.assertEqual(gen.call_args.kwargs["spend"], 4.25)
        self.user.refresh_from_db()
        self.assertEqual((self.user.litellm_key, self.user.litellm_key_id), ("sk-new", "id-new"))

    @mock.patch("chat.litellm_keys.generate_key", return_value=("sk-new", "id-new", None))
    @mock.patch("chat.litellm_keys.key_info", return_value=None)
    def test_missing_old_key_still_issues_new(self, info, gen):
        self.assertEqual(litellm_keys.regenerate_key(self.user), "sk-new")
        self.assertEqual(gen.call_args.kwargs["spend"], 0.0)

    @mock.patch("chat.litellm_keys.generate_key", return_value=("sk-new", "id-new", None))
    def test_user_without_key_gets_one(self, gen):
        self.user.litellm_key = ""
        self.user.save()
        self.assertEqual(litellm_keys.regenerate_key(self.user), "sk-new")
        self.delete.assert_not_called()

    @mock.patch("chat.litellm_keys.generate_key", return_value=("sk-new", "id-new", None))
    @mock.patch("chat.litellm_keys.key_info", return_value={"spend": 0})
    def test_failed_delete_aborts_rotation(self, info, gen):
        self.delete.side_effect = RuntimeError("proxy 500")
        with self.assertRaises(RuntimeError):
            litellm_keys.regenerate_key(self.user)
        gen.assert_not_called()
        self.user.refresh_from_db()
        self.assertEqual(self.user.litellm_key, "sk-old")  # nothing changed


@override_settings(LITELLM_MASTER_KEY="m")
class TeamMembershipTests(TestCase):
    @mock.patch("chat.litellm_keys.httpx.Client")
    def test_already_member_is_ok(self, client_cls):
        client = client_cls.return_value.__enter__.return_value
        client.post.return_value = mock.Mock(status_code=400, text='{"error":"User already in team. Member: ..."}')
        litellm_keys.ensure_team_member("team-1", "s@x.edu")  # no raise
        body = client.post.call_args.kwargs["json"]
        self.assertEqual(body, {"team_id": "team-1", "member": {"user_id": "s@x.edu", "role": "user"}})

    @mock.patch("chat.litellm_keys.httpx.Client")
    def test_other_errors_raise(self, client_cls):
        client = client_cls.return_value.__enter__.return_value
        client.post.return_value = mock.Mock(status_code=500, text="boom")
        with self.assertRaises(RuntimeError):
            litellm_keys.ensure_team_member("team-1", "s@x.edu")

    @mock.patch("chat.litellm_keys.key_info", return_value={"team_id": "team-1", "budget_duration": "1mo"})
    @mock.patch("chat.litellm_keys.ensure_team_member")
    @mock.patch("chat.litellm_keys.httpx.Client")
    @mock.patch("chat.litellm_keys.key_scope_payload", return_value={"models": ["gpt-5"], "team_id": "team-1"})
    @override_settings(LITELLM_KEY_MAX_BUDGET=10.0, LITELLM_KEY_BUDGET_DURATION="1mo")
    def test_update_adds_member_before_key_update(self, _scope, client_cls, member, _info):
        client = client_cls.return_value.__enter__.return_value
        client.post.return_value = mock.Mock(status_code=200, json=lambda: {})
        user = User.objects.create_user(email="s@x.edu", litellm_key="sk-1")
        litellm_keys.update_key_limits(user)
        member.assert_called_once_with("team-1", "s@x.edu")
        self.assertEqual(client.post.call_count, 1)  # same team: no move step
        body = client.post.call_args.kwargs["json"]
        self.assertEqual(body["team_id"], "team-1")
        self.assertNotIn("budget_duration", body)  # unchanged -> not re-sent (would reset the window)

    @mock.patch("chat.litellm_keys.key_info", return_value={"team_id": None, "budget_duration": "30d"})
    @mock.patch("chat.litellm_keys.httpx.Client")
    @mock.patch("chat.litellm_keys.key_scope_payload", return_value={"models": []})
    @override_settings(LITELLM_KEY_MAX_BUDGET=10.0, LITELLM_KEY_BUDGET_DURATION="1mo")
    def test_changed_budget_duration_is_sent(self, _scope, client_cls, _info):
        client = client_cls.return_value.__enter__.return_value
        client.post.return_value = mock.Mock(status_code=200, json=lambda: {})
        user = User.objects.create_user(email="s@x.edu", litellm_key="sk-1")
        litellm_keys.update_key_limits(user)
        self.assertEqual(client.post.call_args.kwargs["json"]["budget_duration"], "1mo")

    @mock.patch("chat.litellm_keys.key_info", return_value={"team_id": "team-old"})
    @mock.patch("chat.litellm_keys.ensure_team_member")
    @mock.patch("chat.litellm_keys.httpx.Client")
    @mock.patch("chat.litellm_keys.key_scope_payload", return_value={"models": ["gpt-5"], "team_id": "team-new"})
    def test_team_change_moves_key_with_empty_models_first(self, _scope, client_cls, member, _info):
        client = client_cls.return_value.__enter__.return_value
        client.post.return_value = mock.Mock(status_code=200, json=lambda: {})
        user = User.objects.create_user(email="s@x.edu", litellm_key="sk-1")
        litellm_keys.update_key_limits(user)
        bodies = [c.kwargs["json"] for c in client.post.call_args_list]
        self.assertEqual(bodies[0], {"key": "sk-1", "models": []})
        self.assertEqual(bodies[1], {"key": "sk-1", "team_id": "team-new"})
        self.assertEqual((bodies[2]["team_id"], bodies[2]["models"]), ("team-new", ["gpt-5"]))
