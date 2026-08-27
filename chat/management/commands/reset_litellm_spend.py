"""
Zero the spend counter on LiteLLM virtual keys so users can continue before
the scheduled monthly reset. Spend history (usage dashboard) is unaffected.

Usage:
    python manage.py reset_litellm_spend --user student@x.edu
    python manage.py reset_litellm_spend --course CSCI-4380-01
    python manage.py reset_litellm_spend --all
"""

from django.core.management.base import BaseCommand, CommandError

from chat import litellm_keys
from chat.models import Course, CustomUser


class Command(BaseCommand):
    help = "Reset the current-period spend on virtual keys (per user, per course, or all)"

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--user", help="Email of a single user")
        group.add_argument("--course", help="Course code: reset every enrolled student and TA")
        group.add_argument("--all", action="store_true", help="Reset every key")

    def handle(self, *args, **options):
        if options["user"]:
            users = list(CustomUser.objects.filter(email=options["user"].strip().lower()))
            if not users:
                raise CommandError(f"No user with email {options['user']}")
        elif options["course"]:
            try:
                course = Course.objects.get(code=options["course"])
            except Course.DoesNotExist:
                raise CommandError(f"No course with code {options['course']}")
            users = [e.user for e in course.enrollments.select_related("user")]
        else:
            users = list(CustomUser.objects.exclude(litellm_key=""))

        result = litellm_keys.reset_spend_for_users(users)
        for email in result["failed"]:
            self.stderr.write(self.style.WARNING(f"  failed: {email}"))
        self.stdout.write(self.style.SUCCESS(f"Reset spend on {result['reset']} key(s); {len(result['failed'])} failed."))
