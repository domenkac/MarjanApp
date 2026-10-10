from datetime import date
from decimal import Decimal
from importlib import import_module
from types import SimpleNamespace

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db
@pytest.mark.parametrize("code,expected", [
    ("OBDAVCENO_45", True), ("OBDAVCENO_STANDARD", False),
])
@pytest.mark.parametrize("valid", [False, True])
def test_explicit_canonical_mapping_must_match_approved_classification(code, expected, valid):
    from django.apps import apps

    from catalogs.models import VatRegime

    migration = import_module("catalogs.migrations.0003_classify_verified_vat_regimes")
    # A verified row queued earlier must remain unknown if any explicit mapping is invalid.
    other_code = "OBDAVCENO_STANDARD" if code == "OBDAVCENO_45" else "OBDAVCENO_45"
    queued = VatRegime.objects.create(
        sifra=other_code, naziv=other_code, nacin_obracuna="OBDAVCENO",
        stopnja=Decimal("22.00"), rekapitulacijska_skupina="OBD_22",
        zahteva_referenco=other_code == "OBDAVCENO_45",
        zahteva_polni_odbitek=None, velja_od=date(2020, 1, 1),
    )
    restriction = expected if valid else not expected
    explicit = VatRegime.objects.create(
        sifra=code, naziv=code, nacin_obracuna="OBDAVCENO",
        stopnja=Decimal("22.00"), rekapitulacijska_skupina="OBD_22",
        zahteva_referenco=code == "OBDAVCENO_45",
        zahteva_polni_odbitek=restriction, velja_od=date(2020, 1, 1),
    )
    if valid:
        migration.classify_verified_regimes(apps, SimpleNamespace(connection=connection))
    else:
        with pytest.raises(RuntimeError, match=code):
            migration.classify_verified_regimes(apps, SimpleNamespace(connection=connection))
    queued.refresh_from_db()
    explicit.refresh_from_db()
    assert queued.zahteva_polni_odbitek is (not expected if valid else None)
    assert explicit.zahteva_polni_odbitek is restriction


@pytest.mark.django_db
@pytest.mark.parametrize("restriction", [False, True])
def test_explicit_custom_migration_mapping_remains_configurable(restriction):
    from django.apps import apps

    from catalogs.models import VatRegime

    row = VatRegime.objects.create(
        sifra="CUSTOM", naziv="Custom", nacin_obracuna="OBDAVCENO",
        stopnja=Decimal("22.00"), rekapitulacijska_skupina="OBD_22",
        zahteva_polni_odbitek=restriction, velja_od=date(2020, 1, 1),
    )
    migration = import_module("catalogs.migrations.0003_classify_verified_vat_regimes")
    migration.classify_verified_regimes(apps, SimpleNamespace(connection=connection))
    row.refresh_from_db()
    assert row.zahteva_polni_odbitek is restriction


@pytest.mark.django_db(transaction=True)
def test_staged_migration_stops_for_unknown_rows_and_preserves_issued_unknowns():
    executor = MigrationExecutor(connection)
    final_targets = executor.loader.graph.leaf_nodes()
    baseline = [("catalogs", "0001_initial"), ("invoicing", "0011_monthlycycle_completion_evidence")]
    baseline.extend(target for target in final_targets if target[0] not in {"catalogs", "invoicing"})
    try:
        executor.migrate(baseline)
        apps = executor.loader.project_state(baseline).apps
        Regime = apps.get_model("catalogs", "VatRegime")
        Company = apps.get_model("companies", "Company")
        Invoice = apps.get_model("invoicing", "Invoice")
        Line = apps.get_model("invoicing", "InvoiceLine")
        company = Company.objects.create(naziv="Zgodovinsko podjetje")
        invoice = Invoice.objects.create(
            company=company, document_type="NP", stevilka="NP-00001",
            status="POSLAN", datum_izdaje=date(2025, 1, 1),
            obdobje_od=date(2025, 1, 1), obdobje_do=date(2025, 1, 31),
        )
        line = Line.objects.create(
            invoice=invoice, opis="Zgodovinska vrstica", osnova=100,
            ddv_stopnja=22, ddv_znesek=22, skupaj=122,
            ddv_nacin_obracuna="OBDAVCENO", ddv_rekap_skupina="OBD_22",
        )
        for code, reference in [("OBDAVCENO_45", True), ("OBDAVCENO_STANDARD", False)]:
            Regime.objects.create(
                sifra=code, naziv=code, nacin_obracuna="OBDAVCENO",
                stopnja=Decimal("22.00"), rekapitulacijska_skupina="OBD_22",
                zahteva_referenco=reference, velja_od=date(2020, 1, 1),
            )
        Regime.objects.create(
            sifra="CUSTOM_RESTRICTED", naziv="Lastni režim", nacin_obracuna="OBDAVCENO",
            stopnja=Decimal("22.00"), rekapitulacijska_skupina="OBD_22",
            zahteva_referenco=True, velja_od=date(2020, 1, 1), aktiven=False,
        )
        stage = [("catalogs", "0002_vatregime_zahteva_polni_odbitek"), *baseline[1:]]
        executor = MigrationExecutor(connection)
        executor.migrate(stage)
        Regime = executor.loader.project_state(stage).apps.get_model("catalogs", "VatRegime")
        assert Regime.objects.filter(zahteva_polni_odbitek__isnull=True).count() == 3
        executor = MigrationExecutor(connection)
        with pytest.raises(RuntimeError, match="CUSTOM_RESTRICTED"):
            executor.migrate([("catalogs", "0003_classify_verified_vat_regimes"), *baseline[1:]])
        assert Regime.objects.filter(zahteva_polni_odbitek__isnull=True).count() == 3
        # Explicit operator mapping after approval; never inferred by the migration.
        Regime.objects.filter(sifra="CUSTOM_RESTRICTED").update(zahteva_polni_odbitek=True)
        executor = MigrationExecutor(connection)
        executor.migrate(final_targets)
        from catalogs.models import VatRegime
        from invoicing.models import Invoice as CurrentInvoice
        from invoicing.models import InvoiceLine

        assert dict(VatRegime.objects.values_list("sifra", "zahteva_polni_odbitek")) == {
            "OBDAVCENO_45": True, "OBDAVCENO_STANDARD": False, "CUSTOM_RESTRICTED": True,
        }
        saved = CurrentInvoice.objects.get(pk=invoice.pk)
        saved_line = InvoiceLine.objects.get(pk=line.pk)
        assert saved.prejemnik_polni_odbitek is None
        assert saved.stevilka == "NP-00001" and saved.status == "POSLAN"
        assert saved_line.ddv_zahteva_polni_odbitek is None and saved_line.ddv_rezim_sifra == ""
        assert saved_line.skupaj == 122
    finally:
        # Restore the schema even if an assertion fails while testing staged upgrades.
        executor = MigrationExecutor(connection)
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM catalogs_vatregime")
        executor.migrate(final_targets)


@pytest.mark.django_db
@pytest.mark.parametrize("changes", [
    {"stopnja": Decimal("9.50")}, {"nacin_obracuna": "OPROSCENO"},
    {"rekapitulacijska_skupina": "CUSTOM"}, {"zahteva_referenco": False},
    {"sifra": "RENAMED_45"}, {"zahteva_polni_odbitek": False},
])
def test_modified_canonical_or_renamed_regime_requires_explicit_mapping(changes):
    from catalogs.models import VatRegime

    data = {
        "sifra": "OBDAVCENO_45", "naziv": "Najem z izjavo",
        "nacin_obracuna": "OBDAVCENO", "stopnja": Decimal("22.00"),
        "rekapitulacijska_skupina": "OBD_22", "zahteva_referenco": True,
        "zahteva_polni_odbitek": None, "velja_od": date(2020, 1, 1),
    }
    row = VatRegime.objects.create(**(data | changes))
    migration = import_module("catalogs.migrations.0003_classify_verified_vat_regimes")
    from django.apps import apps

    with pytest.raises(RuntimeError, match=row.sifra):
        migration.classify_verified_regimes(apps, SimpleNamespace(connection=connection))
    row.refresh_from_db()
    assert row.zahteva_polni_odbitek is changes.get("zahteva_polni_odbitek")
