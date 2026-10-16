import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("invoicing", "0013_invoiceline_adjustment_and_more")]

    operations = [
        migrations.AddField(
            model_name="invoice",
            name="prejemnik",
            field=models.ForeignKey(
                blank=True, editable=False, null=True,
                on_delete=django.db.models.deletion.PROTECT, to="contracts.tenant",
            ),
        ),
        migrations.AddField(
            model_name="invoiceline",
            name="enota_mere",
            field=models.CharField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="invoiceline",
            name="referenca_listine",
            field=models.CharField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="invoiceline",
            name="datum_reference",
            field=models.DateField(blank=True, null=True),
        ),
    ]
