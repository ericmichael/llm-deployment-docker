"""
Re-sync every stored LiteLLM virtual key with the proxy.

For each user with a key this pushes, in one /key/update:
  - user_id + metadata (spend attribution on the usage dashboard)
  - the user's effective budget (user override > course budget > global default)
  - global rate limits and expiry (LITELLM_KEY_*)

Runs automatically at container start (start.sh) so limits can't drift
from settings; safe to re-run at any time.

Usage:
    python manage.py sync_litellm_keys [--dry-run]
"""

from django.core.management.base import BaseCommand

from chat import litellm_keys
from chat.models import Course, CustomUser


class Command(BaseCommand):
    help = "Push spend attribution and effective budget/rate/expiry limits to all LiteLLM virtual keys"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Show what would be sent without updating")

    def handle(self, *args, **options):
        courses = Course.objects.filter(is_active=True)
        for course in courses:
            if options["dry_run"]:
                self.stdout.write(f"  would sync team for {course.code}")
                continue
            try:
                litellm_keys.ensure_team(course)
                self.stdout.write(f"  team ok: {course.code} ({course.litellm_team_id})")
            except Exception as exc:
                self.stderr.write(self.style.WARNING(f"  team failed {course.code}: {exc}"))

        users = CustomUser.objects.exclude(litellm_key="").order_by("email")
        self.stdout.write(f"Found {users.count()} users with keys")

        updated = failed = 0
        for user in users:
            budget, source = litellm_keys.effective_budget_with_source(user)
            label = f"{user.email}: budget ${budget:g} ({source})"
            if options["dry_run"]:
                self.stdout.write(f"  would sync {label}")
                continue
            try:
                litellm_keys.sync_key(user)
                updated += 1
                self.stdout.write(f"  synced {label}")
            except Exception as exc:
                failed += 1
                self.stderr.write(self.style.WARNING(f"  failed {user.email}: {exc}"))

        self.stdout.write(self.style.SUCCESS(f"Done. Synced {updated}, failed {failed}."))
