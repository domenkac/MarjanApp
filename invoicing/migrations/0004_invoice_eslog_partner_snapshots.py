from django.db import migrations, models


def dopolni_eslog_posnetke(apps, schema_editor):
    apps.get_model("invoicing", "Invoice").objects.update(
        izdajatelj_drzava_koda="SI",
        izdajatelj_identificiran_za_ddv=True,
        prejemnik_drzava_koda="SI",
        prejemnik_identificiran_za_ddv=True,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("companies", "0005_company_drzava_koda"),
        ("contracts", "0005_tenant_eslog_podatki"),
        ("invoicing", "0003_eslog_snapshots"),
    ]
    operations = [
        migrations.AddField(
            model_name="invoice",
            name="izdajatelj_drzava_koda",
            field=models.CharField(editable=False, max_length=2, null=True),
        ),
        migrations.AddField(
            model_name="invoice",
            name="izdajatelj_identificiran_za_ddv",
            field=models.BooleanField(editable=False, null=True),
        ),
        migrations.AddField(
            model_name="invoice",
            name="prejemnik_ddv_identifikacijska_stevilka",
            field=models.CharField(blank=True, editable=False, max_length=70),
        ),
        migrations.AddField(
            model_name="invoice",
            name="prejemnik_drzava_koda",
            field=models.CharField(editable=False, max_length=2, null=True),
        ),
        migrations.AddField(
            model_name="invoice",
            name="prejemnik_identificiran_za_ddv",
            field=models.BooleanField(editable=False, null=True),
        ),
        migrations.RunPython(dopolni_eslog_posnetke, migrations.RunPython.noop),
    ]
