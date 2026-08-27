"""Deprecated alias for `sync_litellm_keys` (kept so existing runbooks keep working)."""

from .sync_litellm_keys import Command as SyncCommand


class Command(SyncCommand):
    help = "Deprecated: use sync_litellm_keys"
