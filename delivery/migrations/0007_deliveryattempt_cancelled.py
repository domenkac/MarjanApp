from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("delivery", "0006_deliveryattempt_execution_id")]

    # Add a model choice only. No SQL, table rebuild or historical data rewrite.
    operations = [
        # CANCELLED is permanent audit evidence; do not downgrade its model state.
        migrations.RunPython(migrations.RunPython.noop),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="deliveryattempt",
                    name="result",
                    field=models.CharField(
                        choices=[
                            ("PREPARED", "Prepared"),
                            ("CANCELLED", "Preklicano"),
                            ("SUBMISSION_STARTED", "Submission started"),
                            ("ACCEPTED", "Accepted"),
                            ("CONFIRMED_FAILURE", "Confirmed failure"),
                            ("UNKNOWN", "Unknown"),
                        ],
                        default="PREPARED",
                        max_length=24,
                    ),
                ),
            ],
        ),
    ]
