"""
Keep LiteLLM key access in sync with enrollment state.

Every path that removes an enrollment (UI, admin action, inline delete,
course delete cascade) or deactivates a course ends up here, so keys are
revoked without relying on the manual `revoke_unenrolled_keys` sweep.
"""

import logging

from django.db import transaction
from django.db.models.signals import post_delete, post_save, pre_delete, pre_save
from django.dispatch import receiver

from . import litellm_keys
from .models import Course, CustomUser, Enrollment

logger = logging.getLogger(__name__)


def _after_commit(fn):
    """
    Run a proxy call only once the surrounding DB transaction has committed,
    so a rolled-back save never leaves LiteLLM ahead of the database and
    HTTP calls don't run while row locks are held. Outside a transaction
    this runs immediately.
    """
    transaction.on_commit(fn)


def _safe_revoke(user):
    def run():
        try:
            litellm_keys.revoke_if_unentitled(user)
        except Exception:  # never let proxy trouble block a roster change
            logger.exception("Key revocation check failed for %s", user.email)
    _after_commit(run)


@receiver(post_delete, sender=Enrollment)
def revoke_on_enrollment_delete(sender, instance, **kwargs):
    if getattr(instance, "_moving", False):
        return  # student is being moved to another course; new enrollment follows
    try:
        user = instance.user
    except Enrollment.user.RelatedObjectDoesNotExist:  # user cascade-deleted
        return
    if user.pk is None:
        return
    _safe_revoke(user)


def _safe_sync(user):
    """Push the user's effective limits to their key (after commit), if they have one."""
    def run():
        user.refresh_from_db(fields=["litellm_key"])
        if not user.litellm_key:
            return
        try:
            litellm_keys.sync_key(user)
        except Exception:
            logger.exception("Key limit sync failed for %s", user.email)
    _after_commit(run)


TEAM_FIELDS = ("total_budget", "allowed_models")
MEMBER_KEY_FIELDS = ("monthly_budget", "allowed_models", "litellm_team_id")


@receiver(pre_save, sender=Course)
def _remember_previous_course_state(sender, instance, **kwargs):
    previous = (
        Course.objects.filter(pk=instance.pk)
        .values("is_active", "monthly_budget", "total_budget", "allowed_models", "litellm_team_id")
        .first()
        if instance.pk else None
    )
    instance._previous = previous or {}


def _changed(instance, fields):
    previous = getattr(instance, "_previous", {})
    return any(previous.get(f) != getattr(instance, f) for f in fields)


def _safe_ensure_team(course):
    def run():
        try:
            litellm_keys.ensure_team(course)
        except Exception:
            logger.exception("Team sync failed for %s", course.code)
    _after_commit(run)


@receiver(post_save, sender=Course)
def on_course_saved(sender, instance, created, **kwargs):
    previous = getattr(instance, "_previous", {})
    if not created and previous.get("is_active") and not instance.is_active:
        revoke_keys_for_course(instance)
        # Staff / users enrolled elsewhere keep their key: move it to their new primary course.
        for user in {e.user for e in instance.enrollments.select_related("user")}:
            _safe_sync(user)
        return

    if created or not instance.litellm_team_id or _changed(instance, TEAM_FIELDS):
        _safe_ensure_team(instance)

    reactivated = not created and previous.get("is_active") is False and instance.is_active
    if created:
        return
    if reactivated or _changed(instance, MEMBER_KEY_FIELDS):
        for user in {e.user for e in instance.enrollments.select_related("user")}:
            _safe_sync(user)


@receiver(pre_delete, sender=Course)
def on_course_delete(sender, instance, **kwargs):
    team_id = instance.litellm_team_id
    if not team_id:
        return
    # Local bookkeeping inside the transaction (rolls back with it); the
    # proxy-side delete only once the course is really gone.
    litellm_keys.clear_local_keys(litellm_keys.members_keyed_to(instance))
    _after_commit(lambda: litellm_keys.delete_team_at_proxy(team_id))


@receiver(pre_save, sender=CustomUser)
def _remember_previous_user_budget(sender, instance, **kwargs):
    instance._previous_budget = (
        CustomUser.objects.filter(pk=instance.pk).values_list("monthly_budget", flat=True).first()
        if instance.pk else None
    )


@receiver(post_save, sender=CustomUser)
def sync_on_user_budget_change(sender, instance, created, **kwargs):
    if created:
        return
    if instance.monthly_budget != getattr(instance, "_previous_budget", None):
        _safe_sync(instance)


@receiver(post_save, sender=Enrollment)
def sync_on_enrollment_change(sender, instance, created, **kwargs):
    # Joining/moving courses can change the effective (course) budget.
    _safe_sync(instance.user)


def revoke_keys_for_course(course):
    """Revoke keys of everyone in `course` who no longer has an active enrollment."""
    users = {e.user for e in course.enrollments.select_related("user")}
    for user in users:
        _safe_revoke(user)
