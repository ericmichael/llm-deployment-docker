"""Clear all stored LiteLLM virtual keys so they are regenerated with
unrestricted model access (models=[]) on next login."""

from django.db import migrations


def clear_litellm_keys(apps, schema_editor):
    CustomUser = apps.get_model("chat", "CustomUser")
    CustomUser.objects.filter(litellm_key__gt="").update(
        litellm_key="", litellm_key_id=""
    )


class Migration(migrations.Migration):

    dependencies = [
        ("chat", "0008_add_litellm_keys"),
    ]

    operations = [
        migrations.RunPython(clear_litellm_keys, migrations.RunPython.noop),
    ]
