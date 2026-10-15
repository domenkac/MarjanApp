from calendar import monthrange
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from threading import Barrier

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import IntegrityError, close_old_connections, transaction
from django.test import Client
from django.urls import reverse

from companies.models import Company
from contracts.adjustments import (
    adjustment_review_token,
    decide_adjustment,
    detect_adjustments,
    unresolved_warnings,
)
from contracts.indexation import create_batch
from contracts.models import (
    IndexationEvent,
    Lease,
    LeaseItem,
    PoracunIndeksacije,
    Property,
    Unit,
)
from contracts.services import izracunaj_postavko
from core.models import AuditLogEntry
from invoicing.models import Invoice, InvoiceLine, MonthlyBatch, MonthlyCycle
from tests.contracts.test_indeksacija import postavka  # noqa: F401
from tests.financial_fixtures import save_financial_fixture, update_financial_fixture


def event_for(item, new="110.06", year=2026):
    return IndexationEvent.objects.create(
        lease_item=item,
        datum_uskladitve=date(year, 2, 1),
        indeks_vrednost=Decimal("110.000"),
        stara_vrednost=item.referencna_najemnina,
        nova_vrednost=Decimal(new),
    )


def issued_line(item, month=2, year=2026, net="100.05", proportional=False):
    lease = item.lease
    tenant = lease.tenant
    company = lease.unit.property.company
    invoice = Invoice.objects.create(
        company=company,
        lease=lease,
        document_type="NP",
        status="POSLAN",
        stevilka=f"NP-{Invoice.objects.count() + 1}",
        datum_izdaje=date(year, month, 1),
        obdobje_od=date(year, month, 1),
        obdobje_do=date(year, month, monthrange(year, month)[1]),
        prejemnik_tip=tenant.tip,
        prejemnik_naziv=tenant.naziv,
        prejemnik_naslov=tenant.naslov,
        prejemnik_drzava_koda=tenant.drzava_koda,
        prejemnik_davcna_stevilka=tenant.davcna_stevilka,
        prejemnik_ddv_identifikacijska_stevilka=tenant.ddv_identifikacijska_stevilka,
        prejemnik_maticna_stevilka=tenant.maticna_stevilka,
        prejemnik_identificiran_za_ddv=tenant.identificiran_za_ddv,
        prejemnik_polni_odbitek=tenant.ddv_zavezanec_s_polnim_odbitkom,
        izdajatelj_identificiran_za_ddv=company.ddv_zavezanec,
        izdajatelj_drzava_koda=company.drzava_koda,
        izdajatelj_davcna_stevilka=company.davcna_stevilka,
    )
    net = Decimal(net)
    vat = (net * Decimal("0.22")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return InvoiceLine.objects.create(
        invoice=invoice,
        lease=lease,
        lease_item=item,
        opis="Najemnina",
        osnova=net,
        ddv_stopnja=Decimal("22.00"),
        ddv_znesek=vat,
        skupaj=net + vat,
        ddv_nacin_obracuna="OBDAVCENO",
        ddv_rezim_sifra="DDV22",
        ddv_zahteva_polni_odbitek=False,
        ddv_rekap_skupina="STANDARD",
        ddv_eslog_koda="S",
        je_sorazmerno=proportional,
    )


def detect(event, month=3, **kwargs):
    return detect_adjustments(
        event.lease_item.lease.unit.property.company_id,
        event.pk,
        business_month=date(event.datum_uskladitve.year, month, 1),
        **kwargs,
    )


def reasons(row):
    return {reason["code"] for reason in row.unresolved_reasons}


def review_decision(row, action, reason=""):
    return decide_adjustment(
        row.original_invoice.company_id, row.pk, adjustment_review_token(row), action, reason=reason,
    )


def test_february_march_current_month_cutoff_and_retry(postavka):  # noqa: F811
    issued_line(postavka, month=1)
    february = issued_line(postavka)
    march = issued_line(postavka, month=3)
    issued_line(postavka, month=4)
    event = event_for(postavka)
    rows = detect(event)
    assert [row.source_line_id for row in rows] == [february.pk, march.pk]
    assert all(row.evidence_state == "VERIFIED" for row in rows)
    assert [row.znesek_poracuna for row in rows] == [Decimal("10.01")] * 2
    snapshot = list(PoracunIndeksacije.objects.values())
    audits = list(AuditLogEntry.objects.values())
    assert [row.pk for row in detect(event)] == [row.pk for row in rows]
    assert snapshot == list(PoracunIndeksacije.objects.values())
    assert audits == list(AuditLogEntry.objects.values())
    assert detect(event, month=2)[0].pk == rows[0].pk


@pytest.mark.parametrize(
    "year,start,end,old,new,expected,state",
    [
        (2024, 15, 29, "51.75", "110.06", "5.18", "VERIFIED"),
        (2026, 15, 28, "50.03", "110.06", "5.00", "VERIFIED"),
        (2026, 1, 14, "50.02", "110.05", "5.01", "UNRESOLVED"),
    ],
)
def test_verified_proration_rounds_new_period_before_subtraction(
    postavka,  # noqa: F811
    year,
    start,
    end,
    old,
    new,
    expected,
    state,
):
    # Historical coverage comes from verified evidence, never today's lease dates.
    if year < 2026:
        postavka.datum_referencne_najemnine = date(year - 1, 1, 1)
        save_financial_fixture(postavka, update_fields=["datum_referencne_najemnine"])
        postavka.vat_regime.velja_od = date(year - 1, 1, 1)
        save_financial_fixture(postavka.vat_regime, update_fields=["velja_od"])
    line = issued_line(postavka, year=year, net=old, proportional=True)
    event = event_for(postavka, new=new, year=year)
    row = detect(
        event,
        verified_periods={
            line.pk: {
                "start": date(year, 2, start),
                "end": date(year, 2, end),
                "reference": "Preverjena pogodba in obračun #1",
            }
        },
    )[0]
    assert row.znesek_poracuna == Decimal(expected)
    assert row.evidence_state == state, row.unresolved_reasons
    assert row.calculation_evidence["calculation"]["rounding"] == "ROUND_HALF_UP"
    assert row.prizadeto_obdobje_od == date(year, 2, start)
    assert row.prizadeto_obdobje_do == date(year, 2, end)


def test_missing_partial_dates_never_invent_amount_or_rewrite_evidence(postavka):  # noqa: F811
    line = issued_line(postavka, net="50.03", proportional=True)
    event = event_for(postavka)
    row = detect(event)[0]
    assert row.znesek_poracuna is None and "PERIOD_UNVERIFIED" in reasons(row)
    evidence = row.calculation_evidence
    retry = detect(
        event,
        verified_periods={
            line.pk: {
                "start": date(2026, 2, 15),
                "end": date(2026, 2, 28),
                "reference": "Pogodba",
            }
        },
    )[0]
    assert retry.pk == row.pk and retry.znesek_poracuna is None
    assert retry.calculation_evidence == evidence
    assert "EVIDENCE_CHANGED" in reasons(retry)


@pytest.mark.parametrize(
    "verification",
    [
        {"start": date(2026, 2, 15), "end": date(2026, 2, 14), "reference": "Pogodba"},
        {"start": date(2026, 1, 31), "end": date(2026, 2, 14), "reference": "Pogodba"},
        {"start": date(2026, 2, 15), "end": date(2026, 2, 28), "reference": " "},
    ],
)
def test_invalid_verified_dates_block(postavka, verification):  # noqa: F811
    line = issued_line(postavka, proportional=True)
    row = detect(event_for(postavka), verified_periods={line.pk: verification})[0]
    assert row.znesek_poracuna is None and "PERIOD_UNVERIFIED" in reasons(row)


@pytest.mark.parametrize(
    "status", ["NAPAKA", "REZULTAT_NI_ZNAN", "STORNIRAN", "POPRAVLJEN"]
)
def test_numbered_sources_remain_visible_regardless_delivery_status(postavka, status):  # noqa: F811
    line = issued_line(postavka)
    Invoice.objects.filter(pk=line.invoice_id).update(status=status)
    row = detect(event_for(postavka))[0]
    assert row.source_line_id == line.pk
    assert ("CORRECTION_HISTORY" in reasons(row)) == (
        status in {"STORNIRAN", "POPRAVLJEN"}
    )


@pytest.mark.parametrize(
    "kind,linked", [("STORNO", True), ("DOBROPIS", True), ("STORNO", False)]
)
def test_corrections_require_reconciliation(postavka, kind, linked):  # noqa: F811
    line = issued_line(postavka)
    correction = issued_line(postavka, net="-100.05")
    Invoice.objects.filter(pk=correction.invoice_id).update(
        document_type=kind,
        povezan_racun_id=line.invoice_id if linked else None,
    )
    row = detect(event_for(postavka))[0]
    assert row.evidence_state == "UNRESOLVED" and "CORRECTION_HISTORY" in reasons(row)
    assert correction.invoice_id in [
        doc["id"] for doc in row.calculation_evidence["corrections"]
    ]
    assert row.vkljucen_v_racun_id is None


def test_correction_after_approval_keeps_decision_and_source_evidence(postavka):  # noqa: F811
    line = issued_line(postavka)
    event = event_for(postavka)
    row = detect(event)[0]
    row = review_decision(row, "approve")
    evidence = row.calculation_evidence
    correction = issued_line(postavka)
    Invoice.objects.filter(pk=correction.invoice_id).update(
        document_type="DOBROPIS",
        povezan_racun_id=line.invoice_id,
    )
    retry = detect(event)[0]
    assert retry.status == "POTRJEN" and retry.calculation_evidence == evidence
    assert retry.evidence_state == "UNRESOLVED"
    audits = AuditLogEntry.objects.filter(
        content_type=ContentType.objects.get_for_model(row),
        object_id=row.pk,
    )
    assert audits.count() == 3
    detect(event)
    assert audits.count() == 3


@pytest.mark.parametrize(
    "change",
    [
        {"razlog_rocnega_popravka": "Dogovor"},
        {"izvirno_izracunana_vrednost": Decimal("99.00")},
    ],
)
def test_manual_line_correction_is_unresolved(postavka, change):  # noqa: F811
    line = issued_line(postavka)
    InvoiceLine.objects.filter(pk=line.pk).update(**change)
    row = detect(event_for(postavka))[0]
    assert "MANUAL_CORRECTION" in reasons(row)


@pytest.mark.parametrize(
    "field,value",
    [
        ("ddv_rezim_sifra", ""),
        ("ddv_zahteva_polni_odbitek", None),
        ("ddv_stopnja", Decimal("9.50")),
        ("ddv_klavzula", "Spremenjeno"),
        ("ddv_rekap_skupina", "DRUGO"),
        ("ddv_eslog_koda", "E"),
        ("ddv_znesek", Decimal("1.00")),
    ],
)
def test_uncertain_or_changed_vat_blocks(postavka, field, value):  # noqa: F811
    line = issued_line(postavka)
    InvoiceLine.objects.filter(pk=line.pk).update(**{field: value})
    row = detect(event_for(postavka))[0]
    assert "VAT_UNVERIFIED" in reasons(row) and row.evidence_state == "UNRESOLVED"


@pytest.mark.parametrize(
    "field,value",
    [
        ("prejemnik_identificiran_za_ddv", None),
        ("prejemnik_polni_odbitek", None),
        ("prejemnik_davcna_stevilka", "DRUGI"),
        ("prejemnik_naslov", "Nov naslov"),
        ("izdajatelj_identificiran_za_ddv", None),
    ],
)
def test_incomplete_or_changed_recipient_and_issuer_block(postavka, field, value):  # noqa: F811
    line = issued_line(postavka)
    Invoice.objects.filter(pk=line.invoice_id).update(**{field: value})
    assert "VAT_UNVERIFIED" in reasons(detect(event_for(postavka))[0])


def test_negative_signed_amount_warning_and_lower_future_rent(postavka):  # noqa: F811
    line = issued_line(postavka)
    event = event_for(postavka, new="90.05")
    row = detect(event)[0]
    assert row.znesek_poracuna == Decimal("-10.00")
    assert row.evidence_state == "MANUAL_REVIEW" and "NEGATIVE_ADJUSTMENT" in reasons(
        row
    )
    company_id = line.invoice.company_id
    warning = unresolved_warnings(company_id)[0]
    assert all(
        text in warning
        for text in ["P-1", "01.02.2026", "28.02.2026", "-10.00", "ročni pregled"]
    )
    assert unresolved_warnings(company_id + 100) == []
    batch = create_batch(company_id, 2025, Decimal("110.000"))
    html = (
        Client()
        .get(reverse("ui:indexation_detail", args=[company_id, batch.pk]))
        .content.decode()
    )
    assert warning in html
    assert izracunaj_postavko(
        postavka, date(2026, 4, 1), date(2026, 4, 30)
    ).osnova == Decimal("90.05")
    assert Invoice.objects.count() == 1 and InvoiceLine.objects.count() == 1
    row.vkljucen_v_racun = line.invoice
    with pytest.raises(ValidationError, match="vključevanje"):
        row.save()


def test_multiple_items_same_invoice_and_company_isolation(postavka):  # noqa: F811
    first = issued_line(postavka)
    other = LeaseItem.objects.create(
        lease=postavka.lease,
        naziv_postavke="Parkiranje",
        znesek_neto_mesecno=Decimal("50.00"),
        vat_regime=postavka.vat_regime,
    )
    second = issued_line(other, net="50.00")
    InvoiceLine.objects.filter(pk=second.pk).update(invoice=first.invoice)
    second.invoice.delete()
    e1, e2 = event_for(postavka), event_for(other, new="55.00")
    assert detect(e1)[0].znesek_poracuna == Decimal("10.01")
    assert detect(e2)[0].znesek_poracuna == Decimal("5.00")
    with pytest.raises(IndexationEvent.DoesNotExist):
        detect_adjustments(
            first.invoice.company_id + 1, e1.pk, business_month=date(2026, 3, 1)
        )


def test_duplicate_lines_and_overlapping_originals_block(postavka):  # noqa: F811
    first = issued_line(postavka)
    second = issued_line(postavka)
    event = event_for(postavka)
    rows = detect(event)
    assert len(rows) == 2 and all("AMBIGUOUS_SOURCE" in reasons(row) for row in rows)
    assert {row.source_line_id for row in rows} == {first.pk, second.pk}


def test_evidence_immutable_and_rejected_retry_preserved(postavka):  # noqa: F811
    issued_line(postavka)
    event = event_for(postavka)
    row = detect(event)[0]
    row = review_decision(row, "reject", "Dogovor")
    assert detect(event)[0].razlog_zavrnitve == "Dogovor"
    row.znesek_poracuna = Decimal("999.00")
    with pytest.raises(ValidationError, match="nespremenljiv"):
        row.save()
    with pytest.raises(ValidationError, match="nespremenljiv"):
        PoracunIndeksacije.objects.filter(pk=row.pk).update(calculation_evidence={})
    with pytest.raises(ValidationError, match="nespremenljiv"):
        PoracunIndeksacije.objects.bulk_update([row], ["znesek_poracuna"])
    with pytest.raises(ValidationError, match="Izbris"):
        PoracunIndeksacije.objects.filter(pk=row.pk).delete()


def test_legacy_no_duplicate_or_invented_provenance_and_snapshots_unchanged(postavka):  # noqa: F811
    line = issued_line(postavka)
    event = event_for(postavka)
    legacy = PoracunIndeksacije.objects.create(
        indexation_event=event,
        original_invoice=line.invoice,
        prizadeto_obdobje_od=line.invoice.obdobje_od,
        prizadeto_obdobje_do=line.invoice.obdobje_do,
        znesek_poracuna=Decimal("77.77"),
        status="POTRJEN",
    )
    cycle = MonthlyCycle.objects.create(
        obdobje=date(2026, 3, 1), datum_izdaje=date(2026, 3, 1), trenutni_korak=5
    )
    MonthlyBatch.objects.create(
        company=line.invoice.company,
        cycle=cycle,
        calculation_snapshot={"frozen": "unchanged"},
    )
    before = [
        list(model.objects.values())
        for model in [Invoice, InvoiceLine, MonthlyBatch, PoracunIndeksacije]
    ]
    assert detect(event)[0].pk == legacy.pk
    assert before == [
        list(model.objects.values())
        for model in [Invoice, InvoiceLine, MonthlyBatch, PoracunIndeksacije]
    ]
    assert legacy.source_line_id is None and legacy.calculation_evidence == {}
    assert legacy.evidence_state == "LEGACY_UNVERIFIED"


def test_detection_audit_failure_rolls_back_and_retry_succeeds(postavka, monkeypatch):  # noqa: F811
    issued_line(postavka)
    event = event_for(postavka)
    with monkeypatch.context() as context:

        def fail(*args, **kwargs):
            raise RuntimeError("audit failure")

        context.setattr(AuditLogEntry, "save", fail)
        with pytest.raises(RuntimeError, match="audit failure"):
            detect(event)
    assert not PoracunIndeksacije.objects.exists()
    assert len(detect(event)) == 1


@pytest.mark.parametrize("cutoff", [None, "2026-03-01", date(2026, 3, 15)])
def test_explicit_business_month_required(postavka, cutoff):  # noqa: F811
    event = event_for(postavka)
    with pytest.raises(ValidationError, match="Poslovni mesec"):
        detect_adjustments(
            postavka.lease.unit.property.company_id, event.pk, business_month=cutoff
        )
    assert not PoracunIndeksacije.objects.exists()


def test_two_companies_do_not_share_adjustments_or_warnings(postavka):  # noqa: F811
    company = Company.objects.get(pk=postavka.lease.unit.property.company_id)
    company.pk = None
    company.naziv = "Drugo podjetje"
    company.davcna_stevilka = "12345678"
    save_financial_fixture(company)
    property_ = Property.objects.create(
        company=company, naziv="Druga stavba", naslov="Naslov"
    )
    unit = Unit.objects.create(property=property_, oznaka="2", tip_enote="PISARNA")
    lease = Lease.objects.get(pk=postavka.lease_id)
    lease.pk, lease.unit, lease.oznaka_pogodbe = None, unit, "P-DRUGA"
    lease.save()
    item = LeaseItem.objects.create(
        lease=lease,
        vat_regime=postavka.vat_regime,
        znesek_neto_mesecno=Decimal("100.05"),
        indeks_ob_osnovi=Decimal("100.000"),
        datum_osnove_indeksacije=date(2025, 1, 1),
    )
    own, other = issued_line(postavka), issued_line(item)
    e1, e2 = event_for(postavka, new="90.05"), event_for(item, new="120.05")
    assert detect(e1)[0].znesek_poracuna == Decimal("-10.00")
    assert detect(e2)[0].znesek_poracuna == Decimal("20.00")
    assert unresolved_warnings(company.pk) == []
    assert len(unresolved_warnings(own.invoice.company_id)) == 1
    Invoice.objects.filter(pk=other.invoice_id).update(company=own.invoice.company)
    assert "COMPANY_OR_LEASE_MISMATCH" in reasons(detect(e2)[0])


def test_required_full_deduction_and_reference_not_guessed(postavka):  # noqa: F811
    line = issued_line(postavka)
    regime = postavka.vat_regime
    regime.sifra, regime.zahteva_polni_odbitek = "OBDAVCENO_45", True
    regime.zahteva_referenco = True
    save_financial_fixture(regime)
    InvoiceLine.objects.filter(pk=line.pk).update(
        ddv_rezim_sifra=regime.sifra, ddv_zahteva_polni_odbitek=True
    )
    assert "VAT_UNVERIFIED" in reasons(detect(event_for(postavka))[0])


def test_uncertain_issuance_and_unverified_rent_remain_visible(postavka):  # noqa: F811
    line = issued_line(postavka)
    Invoice.objects.filter(pk=line.invoice_id).update(stevilka=None)
    update_financial_fixture(LeaseItem.objects.filter(pk=postavka.pk), referencna_najemnina=None)
    row = detect(event_for(postavka))[0]
    assert {"ISSUANCE_UNVERIFIED", "RENT_HISTORY_UNVERIFIED"} <= reasons(row)
    assert row.evidence_state == "UNRESOLVED"


def test_partial_header_verified_dates_use_calendar_month_denominator(postavka):  # noqa: F811
    line = issued_line(postavka, net="50.03", proportional=True)
    Invoice.objects.filter(pk=line.invoice_id).update(obdobje_od=date(2026, 2, 15))
    row = detect(
        event_for(postavka),
        verified_periods={
            line.pk: {
                "start": date(2026, 2, 15),
                "end": date(2026, 2, 28),
                "reference": "Preverjeni obračun",
            }
        },
    )[0]
    assert row.evidence_state == "VERIFIED" and row.znesek_poracuna == Decimal("5.00")
    assert row.calculation_evidence["calculation"]["month_days"] == 28


def test_database_identity_constraint_and_protected_source(postavka):  # noqa: F811
    from django.db.models.deletion import ProtectedError

    line = issued_line(postavka)
    row = detect(event_for(postavka))[0]
    duplicate = PoracunIndeksacije.objects.get(pk=row.pk)
    duplicate.pk = None
    duplicate._state.adding = True
    with transaction.atomic(), pytest.raises(IntegrityError):
        duplicate.save()
    with pytest.raises(ProtectedError):
        line.delete()


def test_signed_negative_cannot_be_cleared_or_approved_as_verified(postavka):  # noqa: F811
    issued_line(postavka)
    row = detect(event_for(postavka, new="90.05"))[0]
    row = review_decision(row, "reject", "Zahtevan pregled")
    row.evidence_state = "VERIFIED"
    with pytest.raises(ValidationError):
        row.save()
    row.refresh_from_db()
    row.unresolved_reasons = []
    with pytest.raises(ValidationError):
        row.save()
    row.refresh_from_db()
    assert row.znesek_poracuna == Decimal("-10.00") and row.status == "ZAVRNJEN"
    assert unresolved_warnings(postavka.lease.unit.property.company_id)


def test_new_detection_preserves_frozen_package_and_invoice_snapshots(postavka):  # noqa: F811
    line = issued_line(postavka)
    cycle = MonthlyCycle.objects.create(
        obdobje=date(2026, 3, 1), datum_izdaje=date(2026, 3, 1), trenutni_korak=5
    )
    MonthlyBatch.objects.create(
        company=line.invoice.company,
        cycle=cycle,
        calculation_snapshot={"frozen": "unchanged"},
    )
    models = [Invoice, InvoiceLine, MonthlyBatch, MonthlyCycle]
    originals = [list(model.objects.values()) for model in models]
    event = event_for(postavka)
    detect(event)
    detect(event)
    assert originals == [list(model.objects.values()) for model in models]


def test_retry_detected_negative_persists_signed_observation_and_warning(postavka):  # noqa: F811
    line = issued_line(postavka)
    event = event_for(postavka)
    original = detect(event)[0]
    original = review_decision(original, "approve")
    # Simulate conflicting historical evidence; never an authorized invoice edit.
    InvoiceLine.objects.filter(pk=line.pk).update(
        osnova=Decimal("120.05"),
        ddv_znesek=Decimal("26.41"),
        skupaj=Decimal("146.46"),
    )
    retry = detect(event)[0]
    assert retry.znesek_poracuna == Decimal("10.01") and retry.status == "POTRJEN"
    assert retry.calculation_evidence == original.calculation_evidence
    assert retry.evidence_state == "MANUAL_REVIEW"
    negative = next(
        reason
        for reason in retry.unresolved_reasons
        if reason["code"] == "NEGATIVE_ADJUSTMENT"
    )
    assert negative["amount"] == "-9.99"
    assert "-9.99 EUR" in unresolved_warnings(line.invoice.company_id)[0]
    audit = AuditLogEntry.objects.filter(
        content_type=ContentType.objects.get_for_model(retry),
        object_id=retry.pk,
    ).latest("pk")
    assert '"adjustment_net": "-9.99"' in audit.razlog
    assert detect(event)[0].pk == retry.pk
    assert (
        AuditLogEntry.objects.filter(
            content_type=audit.content_type, object_id=retry.pk
        ).count()
        == 3
    )


@pytest.mark.django_db(transaction=True)
def test_concurrent_detection_is_idempotent(postavka):  # noqa: F811
    issued_line(postavka)
    event = event_for(postavka)
    barrier = Barrier(2)

    def run():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return detect(event)[0].pk
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: run(), range(2)))
    assert ids[0] == ids[1]
    assert (
        PoracunIndeksacije.objects.count() == 1 and AuditLogEntry.objects.count() == 1
    )
