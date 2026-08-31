"""
End-to-end tests against the real LiteLLM proxy (see harness.py).

Run:  python manage.py test chat.tests.e2e
First run records Azure responses into chat/tests/fixtures/cassettes/ and
needs AZURE_OPENAI_* in .env; later runs replay them.
"""

import asyncio
import json
import os
import unittest
from datetime import date, timedelta

import httpx
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from chat import litellm_keys, services, services_usage
from chat.models import Course, CustomUser, Enrollment
from chat.tests.e2e.harness import LiteLLMStack, azure_cassette, has_live_azure


class StackTestCase(TransactionTestCase):
    """Real transactions (signals run on commit) + the shared proxy stack."""

    stack: LiteLLMStack

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.stack = LiteLLMStack.get()
        cls._settings = override_settings(
            LITELLM_PROXY_BASE_URL=cls.stack.proxy_url,
            LITELLM_MASTER_KEY=os.environ["LITELLM_MASTER_KEY"],
            LITELLM_ENABLE_VIRTUAL_KEYS=True,
            LITELLM_KEY_MAX_BUDGET=10.0,
            LITELLM_KEY_BUDGET_DURATION="1mo",
            LITELLM_KEY_DURATION="180d",
        )
        cls._settings.enable()

    @classmethod
    def tearDownClass(cls):
        cls._settings.disable()
        super().tearDownClass()

    # helpers
    def student(self, email, course, **course_kwargs):
        services.add_student_to_course(course, email)
        user = CustomUser.objects.get(email=email)
        key = litellm_keys.ensure_key(user)
        return user, key

    def course(self, code, **kwargs):
        c = Course.objects.create(name=code, code=code, semester="Test", **kwargs)
        c.refresh_from_db()  # team id assigned after commit
        self.assertTrue(c.litellm_team_id, "course should get a LiteLLM team on create")
        return c


class KeyLifecycleTests(StackTestCase):
    def test_key_is_issued_with_limits_and_calendar_month_reset(self):
        c = self.course("KL-1")
        user, key = self.student("kl1@x.edu", c)
        info = litellm_keys.key_info(key)
        self.assertEqual(info["max_budget"], 10.0)
        self.assertEqual(info["budget_duration"], "1mo")
        self.assertEqual(info["rpm_limit"], 60)
        self.assertEqual(info["team_id"], c.litellm_team_id)
        reset_at = litellm_keys._parse_expires(info["budget_reset_at"])
        self.assertEqual(reset_at.day, 1, "monthly reset must land on the 1st")
        self.assertGreater(reset_at, timezone.now())
        expires = litellm_keys._parse_expires(info["expires"])
        self.assertAlmostEqual((expires - timezone.now()).days, 180, delta=1)
        user.refresh_from_db()
        self.assertAlmostEqual(user.litellm_key_expires, expires, delta=timedelta(seconds=1))  # proxy returns ms precision

    def test_second_visit_reuses_key_and_sync_does_not_extend_expiry(self):
        c = self.course("KL-2")
        user, key = self.student("kl2@x.edu", c)
        expires_before = litellm_keys.key_info(key)["expires"]
        reset_before = litellm_keys.key_info(key)["budget_reset_at"]
        self.assertEqual(litellm_keys.ensure_key(user), key)
        litellm_keys.sync_key(user)
        info = litellm_keys.key_info(key)
        self.assertEqual(info["expires"], expires_before)
        self.assertEqual(info["budget_reset_at"], reset_before)

    def test_regenerate_rotates_secret_and_carries_spend(self):
        c = self.course("KL-3")
        user, old = self.student("kl3@x.edu", c)
        httpx.post(f"{self.stack.proxy_url}/key/update", headers=self.stack.master_headers(), json={"key": old, "spend": 1.25})
        new = litellm_keys.regenerate_key(user)
        self.assertNotEqual(new, old)
        self.assertIsNone(litellm_keys.key_info(old), "old key must be dead")
        info = litellm_keys.key_info(new)
        self.assertEqual(info["spend"], 1.25)
        self.assertEqual(info["team_id"], c.litellm_team_id)

    def test_key_deleted_at_proxy_is_reissued(self):
        c = self.course("KL-4")
        user, key = self.student("kl4@x.edu", c)
        httpx.post(f"{self.stack.proxy_url}/key/delete", headers=self.stack.master_headers(), json={"keys": [key]})
        new = litellm_keys.ensure_key(user)
        self.assertNotEqual(new, key)
        self.assertIsNotNone(litellm_keys.key_info(new))

    def test_removing_last_enrollment_revokes_key(self):
        c = self.course("KL-5")
        user, key = self.student("kl5@x.edu", c)
        services.remove_enrollment(Enrollment.objects.get(user=user).pk)
        self.assertIsNone(litellm_keys.key_info(key))
        user.refresh_from_db()
        self.assertEqual(user.litellm_key, "")


class TeamAndScopeTests(StackTestCase):
    def test_team_carries_cap_and_allowlist(self):
        c = self.course("TS-1", total_budget=250, allowed_models=["gpt-4o-mini"])
        team = httpx.get(f"{self.stack.proxy_url}/team/info", headers=self.stack.master_headers(),
                         params={"team_id": c.litellm_team_id}).json()["team_info"]
        self.assertEqual(team["team_alias"], "TS-1")
        self.assertEqual(team["max_budget"], 250.0)
        self.assertEqual(team["models"], ["gpt-4o-mini"])
        self.assertEqual(team["budget_duration"], "1mo")

    def test_key_scoped_to_course_models_and_models_endpoint(self):
        c = self.course("TS-2", allowed_models=["gpt-4o-mini", "gpt-realtime"])
        user, key = self.student("ts2@x.edu", c)
        self.assertEqual(sorted(litellm_keys.key_info(key)["models"]), ["gpt-4o-mini", "gpt-realtime"])
        listed = httpx.get(f"{self.stack.api_url}/models", headers={"Authorization": f"Bearer {key}"}).json()["data"]
        self.assertEqual(sorted(m["id"] for m in listed), ["gpt-4o-mini", "gpt-realtime"])

    def test_disallowed_model_is_rejected_before_reaching_azure(self):
        c = self.course("TS-3", allowed_models=["gpt-4o-mini"])
        user, key = self.student("ts3@x.edu", c)
        r = self.stack.chat(key, "gpt-5")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["error"]["type"], "key_model_access_denied")

    def test_moving_student_rescopes_key_without_rotating_it(self):
        a = self.course("TS-4A", allowed_models=["gpt-4o-mini"])
        b = self.course("TS-4B", allowed_models=["gpt-5"])
        user, key = self.student("ts4@x.edu", a)
        services.add_student_to_course(b, user.email)
        info = litellm_keys.key_info(key)
        self.assertIsNotNone(info, "key must survive the move")
        self.assertEqual(info["team_id"], b.litellm_team_id)
        self.assertEqual(info["models"], ["gpt-5"])
        # course budget follows
        b.monthly_budget = 3
        b.save()
        self.assertEqual(litellm_keys.key_info(key)["max_budget"], 3.0)

    def test_course_delete_removes_team_and_its_keys_only(self):
        a = self.course("TS-5A")
        b = self.course("TS-5B")
        s_user, s_key = self.student("ts5s@x.edu", a)
        services.add_ta_to_course(a, "ts5t@x.edu")
        services.add_student_to_course(b, "ts5t@x.edu")
        t_user = CustomUser.objects.get(email="ts5t@x.edu")
        t_key = litellm_keys.ensure_key(t_user)
        self.assertEqual(litellm_keys.key_info(t_key)["team_id"], b.litellm_team_id)
        team_a = a.litellm_team_id
        a.delete()
        r = httpx.get(f"{self.stack.proxy_url}/team/info", headers=self.stack.master_headers(), params={"team_id": team_a})
        self.assertEqual(r.status_code, 404)
        self.assertIsNone(litellm_keys.key_info(s_key))
        self.assertIsNotNone(litellm_keys.key_info(t_key), "TA keyed to course B keeps their key")

    def test_sync_command_caps_a_legacy_unlimited_key(self):
        c = self.course("TS-6", allowed_models=["gpt-4o-mini"])
        services.add_student_to_course(c, "ts6@x.edu")
        user = CustomUser.objects.get(email="ts6@x.edu")
        # a key from before limits existed: raw generate, no budget/team/models
        raw = httpx.post(f"{self.stack.proxy_url}/key/generate", headers=self.stack.master_headers(),
                         json={"key_alias": user.email, "user_id": user.email}).json()
        CustomUser.objects.filter(pk=user.pk).update(litellm_key=raw["key"], litellm_key_id=raw.get("token_id", ""))
        before = litellm_keys.key_info(raw["key"])
        self.assertIsNone(before["max_budget"])
        from django.core.management import call_command
        call_command("sync_litellm_keys", verbosity=0)
        after = litellm_keys.key_info(raw["key"])
        self.assertEqual(after["max_budget"], 10.0)
        self.assertEqual(after["team_id"], c.litellm_team_id)
        self.assertEqual(after["models"], ["gpt-4o-mini"])


class CompletionAndSpendTests(StackTestCase):
    @azure_cassette("chat_completion_gpt4o_mini")
    def test_completion_flows_through_proxy_and_spend_is_tracked(self):
        c = self.course("CS-1", allowed_models=["gpt-4o-mini"])
        user, key = self.student("cs1@x.edu", c)
        r = self.stack.chat(key, "gpt-4o-mini")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIn("pong", body["choices"][0]["message"]["content"].lower())
        self.assertGreater(body["usage"]["total_tokens"], 0)

        spend = self.stack.wait_for_spend(key, lambda s: s > 0)
        self.assertGreater(spend, 0, "spend must be priced and attributed to the key")

        days = services_usage.get_daily_activity(date.today() - timedelta(days=1), date.today() + timedelta(days=1))
        self.assertTrue(days["success"])
        agg = services_usage.aggregate_from_days(days["days"], filter_emails=[user.email])
        self.assertEqual(agg["by_user"][0]["email"], user.email)
        self.assertGreater(agg["by_user"][0]["total_spend"], 0)

    @azure_cassette("chat_completion_gpt4o_mini_dashboard")
    def test_staff_dashboard_and_settings_page_reflect_spend(self):
        c = self.course("CS-2", allowed_models=["gpt-4o-mini"])
        user, key = self.student("cs2@x.edu", c)
        self.assertEqual(self.stack.chat(key, "gpt-4o-mini").status_code, 200)
        self.stack.wait_for_spend(key, lambda s: s > 0)

        user.set_password("pw"); user.save()
        self.client.force_login(user)
        page = self.client.get(reverse("settings"))
        self.assertContains(page, "of $10.00")
        self.assertContains(page, "gpt-4o-mini")

        staff = CustomUser.objects.create_user(email="cs2staff@x.edu", password="pw", is_staff=True)
        self.client.force_login(staff)
        dash = self.client.get(reverse("usage_dashboard"))
        self.assertContains(dash, user.email)
        self.assertContains(dash, "gpt-4o-mini")

    @azure_cassette("chat_completion_budget")
    def test_budget_is_enforced_and_reset_restores_access(self):
        c = self.course("CS-3", allowed_models=["gpt-4o-mini"], monthly_budget=0.05)
        user, key = self.student("cs3@x.edu", c)
        httpx.post(f"{self.stack.proxy_url}/key/update", headers=self.stack.master_headers(), json={"key": key, "spend": 0.06})
        r = self.stack.chat(key, "gpt-4o-mini")
        self.assertEqual(r.status_code, 429, r.text)
        self.assertEqual(r.json()["error"]["type"], "budget_exceeded")

        self.assertTrue(litellm_keys.reset_spend(user))
        self.assertEqual(litellm_keys.key_info(key)["spend"], 0.0)
        r = self.stack.chat(key, "gpt-4o-mini")
        self.assertEqual(r.status_code, 200, r.text)


class RealtimeTests(StackTestCase):
    """WebSocket sessions can't be recorded by VCR, so this always talks to Azure (~$0.0001)."""

    @unittest.skipUnless(has_live_azure(), "needs AZURE_OPENAI_API_KEY in .env (WebSocket traffic can't be replayed)")
    def test_realtime_session_is_priced_and_counts_toward_budget(self):
        import websockets

        c = self.course("RT-1", allowed_models=["gpt-realtime"])
        user, key = self.student("rt1@x.edu", c)

        async def session():
            url = f"ws://127.0.0.1:{self.stack.port}/v1/realtime?model=gpt-realtime"
            async with websockets.connect(url, additional_headers={"Authorization": f"Bearer {key}"}, max_size=2**24) as ws:
                first = json.loads(await asyncio.wait_for(ws.recv(), 30))
                assert first["type"] == "session.created", first
                await ws.send(json.dumps({"type": "session.update", "session": {"type": "realtime", "output_modalities": ["text"], "instructions": "Reply with one word."}}))
                await ws.send(json.dumps({"type": "conversation.item.create", "item": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Say pong."}]}}))
                await ws.send(json.dumps({"type": "response.create"}))
                while True:
                    ev = json.loads(await asyncio.wait_for(ws.recv(), 60))
                    if ev["type"] == "error":
                        raise AssertionError(ev)
                    if ev["type"] == "response.done":
                        return ev["response"]

        response = asyncio.run(session())
        self.assertEqual(response["status"], "completed", response)
        spend = self.stack.wait_for_spend(key, lambda s: s > 0)
        self.assertGreater(spend, 0, "realtime spend must not be $0 (base_model alias)")


class SettingsPageNumbersTests(StackTestCase):
    """The number under 'Spending this month' must be this month's spend."""

    def test_page_shows_month_to_date_not_the_raw_key_counter(self):
        c = self.course("SP-1", allowed_models=["gpt-4o-mini"])
        user, key = self.student("sp1@x.edu", c)
        # a key carrying spend from before budgets existed
        httpx.post(f"{self.stack.proxy_url}/key/update", headers=self.stack.master_headers(),
                   json={"key": key, "spend": 456.03})
        self.assertEqual(litellm_keys.key_info(key)["spend"], 456.03)

        user.set_password("pw"); user.save()
        self.client.force_login(user)
        page = self.client.get(reverse("settings")).content.decode()

        self.assertNotIn("456.03", page, "must not print the lifetime counter under 'this month'")
        self.assertIn("$0.00", page)          # no calls this month
        self.assertIn("of $10.00", page)
        self.assertIn("Limit reached", page)  # but the key is over budget, so say so

    def test_settings_and_dashboard_agree_on_the_same_user(self):
        c = self.course("SP-2", allowed_models=["gpt-4o-mini"])
        user, _ = self.student("sp2@x.edu", c)
        mtd = services_usage.month_to_date_spend(user.email)
        days = services_usage.get_daily_activity(services_usage.month_start(), date.today())
        agg = services_usage.aggregate_from_days(days["days"], filter_emails=[user.email])
        dash = agg["by_user"][0]["total_spend"] if agg["by_user"] else 0
        self.assertEqual(float(mtd), float(dash))
