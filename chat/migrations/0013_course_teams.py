from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("chat", "0012_monthly_budgets"),
    ]

    operations = [
        migrations.AddField(
            model_name="course",
            name="total_budget",
            field=models.DecimalField(
                blank=True, decimal_places=2, max_digits=10, null=True,
                help_text="USD per month for the whole course (all members combined). Blank = no course-wide cap.",
            ),
        ),
        migrations.AddField(
            model_name="course",
            name="allowed_models",
            field=models.JSONField(
                blank=True, default=list,
                help_text="Model names members may call. Empty = every model the proxy exposes.",
            ),
        ),
        migrations.AddField(
            model_name="course",
            name="litellm_team_id",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
    ]
