from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import migrations, models


def check_legacy_duplicates(apps, schema_editor):
    event = apps.get_model("contracts", "IndexationEvent")
    duplicates = list(
        event.objects.using(schema_editor.connection.alias)
        .filter(izkljucen=False)
        .values("lease_item_id", "datum_uskladitve")
        .annotate(count=models.Count("pk"))
        .filter(count__gt=1)
    )
    if duplicates:
        raise RuntimeError(f"Nerazjasnjene podvojene indeksacije: {duplicates}")


class Migration(migrations.Migration):
    dependencies = [("contracts", "0006_tenant_eslog_neobvezna_polja_and_more")]

    # No legacy amount/date inference. Null references require explicit review.
    # Historical invoice rows, files and monthly JSON are never touched.
    operations = [
        migrations.AddField(
            model_name="leaseitem", name="referencna_najemnina",
            field=models.DecimalField(
                max_digits=10, decimal_places=2, null=True, blank=True,
                validators=[MinValueValidator(Decimal("0.01"))],
            ),
        ),
        migrations.AddField(
            model_name="leaseitem", name="datum_referencne_najemnine",
            field=models.DateField(null=True, blank=True),
        ),
        migrations.RunPython(check_legacy_duplicates, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="indexationevent",
            constraint=models.UniqueConstraint(
                fields=("lease_item", "datum_uskladitve"),
                condition=models.Q(izkljucen=False),
                name="uniq_active_indexation_item_date",
            ),
        ),
    ]
