from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("chat", "0010_make_student_id_optional"),
    ]

    operations = [
        migrations.AddField(
            model_name="customuser",
            name="litellm_key_expires",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
