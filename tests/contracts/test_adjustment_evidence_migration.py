from datetime import date
from decimal import Decimal

import pytest
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from contracts.models import PoracunIndeksacije
from core.models import AuditLogEntry
from invoicing.models import Invoice, InvoiceLine, MonthlyBatch, MonthlyCycle
from tests.contracts.test_indeksacija import postavka  # noqa: F401
from tests.contracts.test_late_adjustments import event_for, issued_line


@pytest.mark.django_db(transaction=True)
def test_additive_evidence_migration_preserves_all_legacy_decisions(postavka):  # noqa: F811
    line = issued_line(postavka)
    event = event_for(postavka)
    included = issued_line(postavka, month=4).invoice
    cycle = MonthlyCycle.objects.create(
        obdobje=date(2026, 3, 1),
        datum_izdaje=date(2026, 3, 1),
        trenutni_korak=5,
    )
    MonthlyBatch.objects.create(
        cycle=cycle,
        company=line.invoice.company,
        calculation_snapshot={"frozen": "legacy evidence"},
    )
    for status, amount in [
        ("PREDLAGAN", "77.77"),
        ("POTRJEN", "-12.34"),
        ("ZAVRNJEN", "9.99"),
    ]:
        row = PoracunIndeksacije.objects.create(
            indexation_event=event,
            original_invoice=line.invoice,
            prizadeto_obdobje_od=line.invoice.obdobje_od,
            prizadeto_obdobje_do=line.invoice.obdobje_do,
            znesek_poracuna=Decimal(amount),
            status=status,
            razlog_zavrnitve="Izrecna zavrnitev" if status == "ZAVRNJEN" else "",
            vkljucen_v_racun=included if status == "POTRJEN" else None,
        )
        AuditLogEntry.objects.create(
            content_type=ContentType.objects.get_for_model(row),
            object_id=row.pk,
            polje="status",
            stara_vrednost="PREDLAGAN",
            nova_vrednost=status,
            razlog="Obstoječa odločitev",
        )
    originals = [
        list(model.objects.order_by("pk").values())
        for model in [Invoice, InvoiceLine, MonthlyBatch, AuditLogEntry]
    ]
    latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
    old = [
        (app, "0008_indexationbatch_indexationdecision")
        if app == "contracts"
        else (app, name)
        for app, name in latest
    ]
    try:
        MigrationExecutor(connection).migrate(old)
        apps = MigrationExecutor(connection).loader.project_state(old).apps
        Legacy = apps.get_model("contracts", "PoracunIndeksacije")
        legacy = list(Legacy.objects.order_by("pk").values())
        assert len(legacy) == 3
        MigrationExecutor(connection).migrate(latest)
        saved = list(PoracunIndeksacije.objects.order_by("pk").values())
        for original, upgraded in zip(legacy, saved, strict=True):
            assert {key: upgraded[key] for key in original} == original
            assert upgraded["source_line_id"] is None
            assert upgraded["calculation_evidence"] == {}
            assert (
                upgraded["detected_at"] is None and upgraded["detection_month"] is None
            )
            assert upgraded["evidence_state"] == "LEGACY_UNVERIFIED"
            assert upgraded["last_observation_fingerprint"] == ""
        assert originals == [
            list(model.objects.order_by("pk").values())
            for model in [Invoice, InvoiceLine, MonthlyBatch, AuditLogEntry]
        ]
    finally:
        MigrationExecutor(connection).migrate(latest)
