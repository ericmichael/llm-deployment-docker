from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("chat", "0009_clear_litellm_keys"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="enrollment",
            name="student_id",
        ),
    ]
