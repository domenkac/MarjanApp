from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("catalogs", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="vatregime", name="zahteva_polni_odbitek",
            field=models.BooleanField(null=True),
        ),
    ]
