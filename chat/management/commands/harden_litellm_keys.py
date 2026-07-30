"""
Apply the configured budget/rate/expiry limits to all existing LiteLLM keys.

Keys issued before limits existed have no budget, no rate limit, and no
expiry. This backfills the current LITELLM_KEY_* settings onto every stored
key via /key/update.

Usage:
    python manage.py harden_litellm_keys [--dry-run]
"""

from django.core.management.base import BaseCommand

from chat import litellm_keys
from chat.models import CustomUser


class Command(BaseCommand):
    help = "Apply configured budget/rate/expiry limits to all existing LiteLLM virtual keys"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="List keys without updating")

    def handle(self, *args, **options):
        limits = litellm_keys.key_limit_payload()
        if not limits:
            self.stderr.write(self.style.ERROR("No key limits configured (LITELLM_KEY_*)"))
            return

        self.stdout.write(f"Limits to apply: {limits}")

        users = CustomUser.objects.exclude(litellm_key="")
        self.stdout.write(f"Found {users.count()} users with keys")

        updated = failed = 0
        for user in users:
            if options["dry_run"]:
                self.stdout.write(f"  would update: {user.email}")
                continue
            try:
                litellm_keys.update_key_limits(user)
                updated += 1
                self.stdout.write(f"  updated: {user.email}")
            except (RuntimeError, Exception) as exc:
                failed += 1
                self.stderr.write(self.style.WARNING(f"  failed: {user.email}: {exc}"))

        self.stdout.write(self.style.SUCCESS(f"Done. Updated {updated}, failed {failed}."))
