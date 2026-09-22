from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("invoicing", "0008_monthlybatch_calculation_state")]

    operations = [
        migrations.AddField(
            model_name="invoice",
            name="dostavni_kontakti_snapshot",
            field=models.JSONField(default=dict, editable=False),
        ),
    ]
