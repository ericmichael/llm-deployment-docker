"""
Management command to backfill user_id on existing LiteLLM virtual keys.

Usage:
    python manage.py fix_litellm_keys

This fixes keys that were created without a user_id, which causes spend
tracking to show users as "unknown" in the usage dashboard.
"""

import httpx
from django.conf import settings
from django.core.management.base import BaseCommand

from chat.models import CustomUser


class Command(BaseCommand):
    help = "Set user_id on existing LiteLLM virtual keys for spend tracking"

    def handle(self, *args, **options):
        base_url = getattr(settings, "LITELLM_PROXY_BASE_URL", None)
        if not base_url:
            base_url = "http://localhost:8000/litellm"
        base_url = base_url.rstrip("/")

        master_key = getattr(settings, "LITELLM_MASTER_KEY", None)
        if not master_key:
            self.stderr.write(self.style.ERROR("LITELLM_MASTER_KEY not set"))
            return

        headers = {
            "Authorization": f"Bearer {master_key}",
            "Content-Type": "application/json",
        }

        users = CustomUser.objects.exclude(litellm_key="").exclude(litellm_key_id="")
        self.stdout.write(f"Found {users.count()} users with LiteLLM keys")

        updated = 0
        skipped = 0
        errors = 0

        with httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
            for user in users:
                try:
                    resp = client.post(
                        f"{base_url}/key/update",
                        headers=headers,
                        json={
                            "key": user.litellm_key,
                            "user_id": user.email,
                            "metadata": {
                                "django_user_id": str(user.id),
                                "email": user.email,
                            },
                        },
                    )
                    if resp.status_code < 400:
                        updated += 1
                        self.stdout.write(f"  Updated: {user.email}")
                    else:
                        errors += 1
                        self.stderr.write(
                            self.style.WARNING(f"  Failed: {user.email} - {resp.status_code} {resp.text[:200]}")
                        )
                except Exception as exc:
                    errors += 1
                    self.stderr.write(self.style.WARNING(f"  Error: {user.email} - {exc}"))

        self.stdout.write(
            self.style.SUCCESS(f"Done. Updated: {updated}, Errors: {errors}")
        )
