from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("chat", "0011_customuser_litellm_key_expires"),
    ]

    operations = [
        migrations.AddField(
            model_name="course",
            name="monthly_budget",
            field=models.DecimalField(
                blank=True, decimal_places=2, max_digits=8, null=True,
                help_text="USD per 30 days for students in this course. Blank = global default. 0 = unlimited.",
            ),
        ),
        migrations.AddField(
            model_name="customuser",
            name="monthly_budget",
            field=models.DecimalField(
                blank=True, decimal_places=2, max_digits=8, null=True,
                help_text="USD per 30 days. Overrides the course and global budgets. 0 = unlimited.",
            ),
        ),
    ]
