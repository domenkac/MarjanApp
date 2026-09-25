from django.db import migrations, models


def rename_failed_result(apps, schema_editor):
    DeliveryAttempt = apps.get_model("delivery", "DeliveryAttempt")
    DeliveryAttempt.objects.filter(result="FAILED_BEFORE_SUBMISSION").update(
        result="CONFIRMED_FAILURE"
    )


def restore_failed_result(apps, schema_editor):
    DeliveryAttempt = apps.get_model("delivery", "DeliveryAttempt")
    DeliveryAttempt.objects.filter(result="CONFIRMED_FAILURE").update(
        result="FAILED_BEFORE_SUBMISSION"
    )


class Migration(migrations.Migration):
    dependencies = [("delivery", "0001_initial")]

    operations = [
        migrations.RunPython(rename_failed_result, restore_failed_result),
        migrations.AlterField(
            model_name="deliveryattempt",
            name="result",
            field=models.CharField(
                choices=[
                    ("PREPARED", "Prepared"),
                    ("SUBMISSION_STARTED", "Submission started"),
                    ("ACCEPTED", "Accepted"),
                    ("CONFIRMED_FAILURE", "Confirmed failure"),
                    ("UNKNOWN", "Unknown"),
                ],
                default="PREPARED",
                max_length=24,
            ),
        ),
    ]
