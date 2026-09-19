from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("invoicing", "0002_pdf_snapshots"),
    ]

    operations = [
        migrations.AddField(
            model_name="invoice",
            name="prejemnik_tip",
            field=models.CharField(
                blank=True,
                choices=[
                    ("PRAVNA_OSEBA", "Pravna oseba"),
                    ("FIZICNA_OSEBA", "Fizična oseba"),
                ],
                default="",
                max_length=13,
            ),
        ),
        migrations.AddField(
            model_name="invoice",
            name="rendered_eslog_checksum",
            field=models.CharField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="invoiceline",
            name="ddv_eslog_koda",
            field=models.CharField(blank=True, default=""),
        ),
    ]
