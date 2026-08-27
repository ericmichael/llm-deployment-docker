from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

User = get_user_model()


@override_settings(EASYAUTH_ENABLED=True, LITELLM_ENABLE_VIRTUAL_KEYS=False)
class EasyAuthMiddlewareTests(TestCase):
    def test_header_logs_in_and_creates_user(self):
        resp = self.client.get(reverse("settings"), HTTP_X_MS_CLIENT_PRINCIPAL_NAME="New@X.edu")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(User.objects.filter(email="new@x.edu").exists())

    def test_inactive_user_is_not_logged_in(self):
        User.objects.create_user(email="gone@x.edu", is_active=False)
        resp = self.client.get(reverse("settings"), HTTP_X_MS_CLIENT_PRINCIPAL_NAME="gone@x.edu")
        self.assertEqual(resp.status_code, 302)  # bounced to login
        self.assertNotIn("_auth_user_id", self.client.session)

    @override_settings(EASYAUTH_ENABLED=False)
    def test_header_ignored_when_disabled(self):
        resp = self.client.get(reverse("settings"), HTTP_X_MS_CLIENT_PRINCIPAL_NAME="spoof@x.edu")
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(User.objects.filter(email="spoof@x.edu").exists())


class HealthCheckTests(TestCase):
    def test_health_does_not_echo_internals(self):
        resp = self.client.get(reverse("health_check"))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["checks"]["database"], "ok")
        self.assertNotIn("localhost", str(body["checks"]["litellm"]))
