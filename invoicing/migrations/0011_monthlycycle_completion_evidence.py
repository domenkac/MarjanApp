from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("invoicing", "0010_monthlycandidatereview"),
    ]

    operations = [
        migrations.AddField(
            model_name="monthlycycle",
            name="completed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="monthlycycle",
            name="completion_snapshot",
            field=models.JSONField(blank=True, null=True),
        ),
    ]
