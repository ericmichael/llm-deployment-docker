"""Test runner that shuts the in-process LiteLLM stack down before the test DB is dropped."""

from django.test.runner import DiscoverRunner


class StackAwareRunner(DiscoverRunner):
    def teardown_databases(self, old_config, **kwargs):
        from chat.tests.e2e.harness import LiteLLMStack

        if LiteLLMStack._instance is not None:
            LiteLLMStack._instance.stop()
            LiteLLMStack._instance = None
            # LiteLLM's Prisma pool may still hold sessions on the test DB.
            from django.db import connection

            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    [connection.settings_dict["NAME"]],
                )
        super().teardown_databases(old_config, **kwargs)
