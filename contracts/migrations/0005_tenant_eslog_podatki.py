from django.db import migrations, models

import core.validators


def dopolni_eslog_podatke(apps, schema_editor):
    apps.get_model("contracts", "Tenant").objects.update(
        drzava_koda="SI", identificiran_za_ddv=True
    )


class Migration(migrations.Migration):
    dependencies = [("contracts", "0004_poracunindeksacije")]
    operations = [
        migrations.AddField(
            model_name="tenant",
            name="ddv_identifikacijska_stevilka",
            field=models.CharField(blank=True, max_length=70),
        ),
        migrations.AddField(
            model_name="tenant",
            name="drzava_koda",
            field=models.CharField(max_length=2, null=True),
        ),
        migrations.AddField(
            model_name="tenant",
            name="identificiran_za_ddv",
            field=models.BooleanField(null=True),
        ),
        migrations.RunPython(dopolni_eslog_podatke, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="tenant",
            name="davcna_stevilka",
            field=models.CharField(blank=True, max_length=70),
        ),
        migrations.AlterField(
            model_name="tenant",
            name="maticna_stevilka",
            field=models.CharField(blank=True, max_length=70),
        ),
        migrations.AlterField(
            model_name="tenant",
            name="drzava_koda",
            field=models.CharField(
                max_length=2,
                validators=[core.validators.preveri_kodo_drzave],
            ),
        ),
        migrations.AlterField(
            model_name="tenant",
            name="identificiran_za_ddv",
            field=models.BooleanField(),
        ),
    ]
