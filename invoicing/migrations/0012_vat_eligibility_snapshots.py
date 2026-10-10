from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("invoicing", "0011_monthlycycle_completion_evidence"),
        ("catalogs", "0003_classify_verified_vat_regimes"),
    ]

    operations = [
        migrations.AddField(
            model_name="invoice", name="prejemnik_polni_odbitek",
            field=models.BooleanField(null=True, blank=True, editable=False),
        ),
        migrations.AddField(
            model_name="invoiceline", name="ddv_rezim_sifra",
            field=models.CharField(blank=True, default="", editable=False),
        ),
        migrations.AddField(
            model_name="invoiceline", name="ddv_zahteva_polni_odbitek",
            field=models.BooleanField(null=True, blank=True, editable=False),
        ),
    ]
