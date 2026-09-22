from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("invoicing", "0007_monthlybatch_preflight_checked_at_and_more")]

    operations = [
        migrations.AddField(
            model_name="monthlybatch",
            name="calculation_status",
            field=models.CharField(
                choices=[
                    ("NOT_CALCULATED", "Ni izračunano"),
                    ("CALCULATED", "Izračunano"),
                    ("BLOCKED", "Blokirano"),
                ],
                default="NOT_CALCULATED",
                max_length=14,
            ),
        ),
        migrations.AddField(
            model_name="monthlybatch",
            name="calculated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="monthlybatch",
            name="calculation_schema_version",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="monthlybatch",
            name="calculation_snapshot",
            field=models.JSONField(default=dict),
        ),
    ]
