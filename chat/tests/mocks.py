"""Test helper: silence all outbound LiteLLM calls triggered by model signals."""

from unittest import mock

PROXY_FUNCS = ("ensure_team", "delete_team_at_proxy", "sync_key", "revoke_key", "team_usage")


class ProxyMockMixin:
    """Patches the signal-driven LiteLLM calls for the whole test (incl. setUp)."""

    def _pre_setup(self):
        super()._pre_setup()
        self.proxy = {}
        for name in PROXY_FUNCS:
            patcher = mock.patch(f"chat.litellm_keys.{name}", return_value=None if name == "team_usage" else True)
            self.proxy[name] = patcher.start()
            self.addCleanup(patcher.stop)
        # TestCase never commits, so execute on_commit hooks inline
        oc = mock.patch("chat.signals._after_commit", side_effect=lambda fn: fn())
        oc.start()
        self.addCleanup(oc.stop)
        # course pages list proxy models; serve the config's names instead of calling out
        info = mock.patch("chat.services_models.get_model_info", return_value={"success": False, "models": [], "message": ""})
        info.start()
        self.addCleanup(info.stop)
