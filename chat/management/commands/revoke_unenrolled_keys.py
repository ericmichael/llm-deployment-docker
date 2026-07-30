"""
Revoke LiteLLM keys held by users with no active enrollment.

Single-enrollment removals revoke keys automatically; this command sweeps
the rest - e.g. after deactivating a whole course at semester end.

Usage:
    python manage.py revoke_unenrolled_keys [--dry-run]
"""

from django.core.management.base import BaseCommand

from chat import litellm_keys
from chat.models import CustomUser


class Command(BaseCommand):
    help = "Revoke LiteLLM keys for users without an active enrollment (staff keep theirs)"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="List keys without revoking")

    def handle(self, *args, **options):
        users = CustomUser.objects.exclude(litellm_key="")
        revoked = kept = 0

        for user in users:
            if litellm_keys.user_keeps_key(user):
                kept += 1
                continue
            if options["dry_run"]:
                self.stdout.write(f"  would revoke: {user.email}")
                continue
            litellm_keys.revoke_key(user)
            revoked += 1
            self.stdout.write(f"  revoked: {user.email}")

        self.stdout.write(
            self.style.SUCCESS(f"Done. Revoked {revoked}, kept {kept} (staff/enrolled).")
        )
