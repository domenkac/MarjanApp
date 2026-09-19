from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("invoicing", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="invoice",
            name="izdajatelj_logotip_slika_pot",
            field=models.CharField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="invoice",
            name="pdf_template_version",
            field=models.CharField(blank=True, default=""),
        ),
    ]