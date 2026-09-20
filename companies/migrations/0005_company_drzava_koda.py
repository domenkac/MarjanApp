from django.db import migrations, models

import core.validators


def dopolni_drzavo(apps, schema_editor):
    apps.get_model("companies", "Company").objects.update(drzava_koda="SI")


class Migration(migrations.Migration):
    dependencies = [
        ("companies", "0004_alter_company_logotip_alter_signatory_podpis_slika")
    ]
    operations = [
        migrations.AddField(
            model_name="company",
            name="drzava_koda",
            field=models.CharField(max_length=2, null=True),
        ),
        migrations.RunPython(dopolni_drzavo, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="company",
            name="drzava_koda",
            field=models.CharField(
                max_length=2,
                validators=[core.validators.preveri_kodo_drzave],
            ),
        ),
    ]
