from decimal import Decimal

from django.db import migrations

# Only the two recipient classifications approved for T1 (PRD §6.3).
CANONICAL = {
    "OBDAVCENO_45": ("OBDAVCENO", Decimal("22.00"), "OBD_22", True, True),
    "OBDAVCENO_STANDARD": ("OBDAVCENO", Decimal("22.00"), "OBD_22", False, False),
}


def classify_verified_regimes(apps, schema_editor):
    Regime = apps.get_model("catalogs", "VatRegime")
    rows = Regime.objects.using(schema_editor.connection.alias)
    verified = []
    unresolved = []
    for row in rows.order_by("pk"):
        expected = CANONICAL.get(row.sifra)
        signature = (
            row.nacin_obracuna, row.stopnja,
            row.rekapitulacijska_skupina, row.zahteva_referenco,
        )
        if row.zahteva_polni_odbitek is not None:
            if expected is not None and row.zahteva_polni_odbitek is not expected[4]:
                unresolved.append(f"{row.pk}:{row.sifra}")
            continue  # An explicit classification supplied after stage 0002.
        if expected is None or signature != expected[:4]:
            unresolved.append(f"{row.pk}:{row.sifra}")
        else:
            verified.append((row.pk, expected[4]))
    if unresolved:
        raise RuntimeError(
            "Nerazrešena razvrstitev DDV režimov (id:šifra): "
            + ", ".join(unresolved)
            + ". Ustavite nadgradnjo; potrebna je izrecna potrjena preslikava. "
            "Ne uporabite privzete vrednosti False."
        )
    for pk, restriction in verified:
        rows.filter(pk=pk).update(zahteva_polni_odbitek=restriction)


class Migration(migrations.Migration):
    dependencies = [("catalogs", "0002_vatregime_zahteva_polni_odbitek")]

    operations = [migrations.RunPython(classify_verified_regimes, migrations.RunPython.noop)]
