from datetime import date
from decimal import Decimal

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("duplicate", [False, True])
def test_additive_migration_preserves_legacy_and_stops_duplicates(duplicate):
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    old = [(app, "0006_tenant_eslog_neobvezna_polja_and_more") if app == "contracts" else (app, name) for app, name in latest]
    try:
        executor.migrate(old)
        apps = executor.loader.project_state(old).apps
        company = apps.get_model("companies", "Company").objects.create(naziv="Legacy")
        property_ = apps.get_model("contracts", "Property").objects.create(company=company, naziv="Stavba", naslov="Naslov")
        unit = apps.get_model("contracts", "Unit").objects.create(property=property_, oznaka="1", tip_enote="PISARNA")
        tenant = apps.get_model("contracts", "Tenant").objects.create(naziv="Najemnik", tip="PRAVNA_OSEBA", identificiran_za_ddv=False)
        lease = apps.get_model("contracts", "Lease").objects.create(
            unit=unit, tenant=tenant, oznaka_pogodbe="Legacy", datum_zacetka=date(2020, 1, 1),
            tip_trajanja="NEDOLOCEN_CAS", rok_placila_dni=15, ima_klavzulo_indeksacije=True,
            tip_pogoja_indeksacije="VEDNO",
        )
        regime = apps.get_model("catalogs", "VatRegime").objects.create(
            sifra="LEGACY", naziv="Legacy", nacin_obracuna="OBDAVCENO", stopnja=Decimal(22),
            rekapitulacijska_skupina="STANDARD", velja_od=date(2020, 1, 1),
        )
        item = apps.get_model("contracts", "LeaseItem").objects.create(
            lease=lease, vat_regime=regime, znesek_neto_mesecno=Decimal(150),
            indeks_ob_osnovi=Decimal(100), datum_osnove_indeksacije=date(2020, 1, 1),
        )
        Event = apps.get_model("contracts", "IndexationEvent")
        event_data = {
            "lease_item": item, "datum_uskladitve": date(2026, 2, 1), "indeks_vrednost": Decimal(110),
            "stara_vrednost": Decimal(100), "nova_vrednost": Decimal(110),
        }
        event = Event.objects.create(**event_data)
        extra = Event.objects.create(**event_data) if duplicate else None
        Invoice = apps.get_model("invoicing", "Invoice")
        invoice = Invoice.objects.create(
            company=company, lease=lease, document_type="NP", stevilka="NP-00042", status="POSLAN",
            datum_izdaje=date(2026, 2, 1), obdobje_od=date(2026, 2, 1), obdobje_do=date(2026, 2, 28),
            rendered_pdf_path="legacy.pdf", checksum="legacy-checksum",
        )
        line = apps.get_model("invoicing", "InvoiceLine").objects.create(
            invoice=invoice, lease=lease, lease_item=item, opis="Legacy", osnova=Decimal(150),
            ddv_stopnja=Decimal(22), ddv_znesek=Decimal(33), skupaj=Decimal(183),
            ddv_nacin_obracuna="OBDAVCENO", ddv_rekap_skupina="STANDARD",
        )
        cycle = apps.get_model("invoicing", "MonthlyCycle").objects.create(obdobje=date(2026, 3, 1), datum_izdaje=date(2026, 3, 1), trenutni_korak=5)
        batch = apps.get_model("invoicing", "MonthlyBatch").objects.create(
            cycle=cycle, company=company, calculation_schema_version=4,
            calculation_snapshot={"candidates": [{"candidate_fingerprint": "legacy-fingerprint"}]},
        )
        originals = (Invoice.objects.values().get(pk=invoice.pk), line.__class__.objects.values().get(pk=line.pk), batch.__class__.objects.values().get(pk=batch.pk))
        if duplicate:
            with pytest.raises(RuntimeError, match="podvojene"):
                MigrationExecutor(connection).migrate(latest)
            assert Event.objects.count() == 2
            assert Invoice.objects.values().get(pk=invoice.pk) == originals[0]
            # Test-only explicit resolution, never part of the migration.
            extra.izkljucen = True
            extra.razlog_izkljucitve = "Preverjen podvojen zapis"
            extra.save()
        MigrationExecutor(connection).migrate(latest)
        from django.core.exceptions import ValidationError

        from contracts.models import IndexationEvent, LeaseItem
        from contracts.services import najemnina_na_datum
        from invoicing.models import Invoice as CurrentInvoice
        from invoicing.models import InvoiceLine, MonthlyBatch

        saved = LeaseItem.objects.get(pk=item.pk)
        assert saved.referencna_najemnina is None
        assert saved.datum_referencne_najemnine is None
        assert saved.znesek_neto_mesecno == Decimal(150)
        with pytest.raises(ValidationError, match="nerazjasnjena"):
            najemnina_na_datum(saved, date(2026, 2, 1))
        assert IndexationEvent.objects.get(pk=event.pk).stara_vrednost == Decimal(100)
        assert CurrentInvoice.objects.values().get(pk=invoice.pk) == originals[0]
        assert InvoiceLine.objects.values().get(pk=line.pk) == originals[1]
        assert MonthlyBatch.objects.values().get(pk=batch.pk) == originals[2]
    finally:
        MigrationExecutor(connection).migrate(latest)
