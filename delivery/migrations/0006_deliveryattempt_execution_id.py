from django.db import migrations, models

# Stop all pre-T5 processes before upgrade: legacy sends do not own execution locks.


class Migration(migrations.Migration):
    dependencies = [("delivery", "0005_accountant_report_delivery_attempt")]

    operations = [
        migrations.AddField(
            model_name="deliveryattempt",
            name="execution_id",
            field=models.UUIDField(blank=True, editable=False, null=True),
        ),
    ]
