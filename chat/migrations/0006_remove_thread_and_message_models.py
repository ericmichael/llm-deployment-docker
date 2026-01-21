# Generated manually - Remove Thread and Message models

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("chat", "0005_alter_thread_model_alter_thread_temperature"),
    ]

    operations = [
        migrations.DeleteModel(
            name="Message",
        ),
        migrations.DeleteModel(
            name="Thread",
        ),
    ]
