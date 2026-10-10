from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from accounting_export.csv_renderer import render_accountant_report_csv
from accounting_export.models import AccountantReport
from accounting_export.services import create_accountant_report
from archive.models import ArchivedFile
from catalogs.models import VatRegime
from companies.models import Company
from contracts.models import Lease, LeaseItem, Property, Tenant, Unit
from delivery.models import DeliveryAttempt, MonthlySummaryAttempt
from invoicing.models import (
    Invoice,
    InvoiceLine,
    MonthlyBatch,
    MonthlyCandidateReview,
    MonthlyCycle,
)
from invoicing.services import CALCULATION_SNAPSHOT_VERSION, _candidate_fingerprint


@pytest.fixture
def company(db):
    return Company.objects.create(
        drzava_koda="SI",
        naziv="Živa Alfa",
        davcna_stevilka="15012557",
        maticna_stevilka="1234567000",
        naslov="Glavna 1",
        trr="SI56191000000123438",
        logotip="logo.png",
        email_racunovodja="rac@example.com",
        email_posiljatelj="racuni@example.com",
        keychain_kljuc_posiljatelja="alfa",
    )


def _snapshot(company, candidates=None):
    return {
        "schema_version": CALCULATION_SNAPSHOT_VERSION,
        "period_start": "2026-09-01",
        "period_end": "2026-09-30",
        "issue_date": "2026-09-05",
        "pdf_template_version": "1",
        "company": {
            "id": company.pk,
            "name": "Zgodovinska Alfa",
            "address": "Glavna 1",
            "country": "SI",
            "vat_registered": True,
            "tax_number": "15012557",
            "registration_number": "1234567000",
            "iban": "SI56191000000123438",
            "logo_checksum": "0" * 64,
            "signatory": {
                "name": "Podpisnik",
                "function": "Direktor",
                "signature_checksum": "1" * 64,
            },
        },
        "candidates": candidates or [],
    }


def _ensure_completed_batch(company, period=date(2026, 9, 1)):
    cycle, _ = MonthlyCycle.objects.get_or_create(
        obdobje=period,
        defaults={
            "datum_izdaje": date(2026, 9, 5),
            "trenutni_korak": MonthlyCycle.Korak.ZAKLJUCEK,
            "status": MonthlyCycle.Status.ZAKLJUCEN,
        },
    )
    batch, _ = MonthlyBatch.objects.get_or_create(
        cycle=cycle,
        company=company,
        defaults={
            "calculation_status": MonthlyBatch.CalculationStatus.CALCULATED,
            "calculation_schema_version": CALCULATION_SNAPSHOT_VERSION,
            "calculation_snapshot": _snapshot(company),
        },
    )
    return batch


def _invoice(
    company,
    *,
    number="NP-00001",
    kind="NP",
    issued=date(2026, 9, 5),
    completed_evidence=True,
    **changes,
):
    if completed_evidence:
        _ensure_completed_batch(company, issued.replace(day=1))
    values = {
        "company": company,
        "document_type": kind,
        "stevilka": number,
        "datum_izdaje": issued,
        "obdobje_od": date(2026, 9, 1),
        "obdobje_do": date(2026, 9, 30),
        "prejemnik_naziv": "Stari najemnik",
        "prejemnik_davcna_stevilka": "87654321",
        "prejemnik_maticna_stevilka": "7654321000",
        "izdajatelj_naziv": "Zgodovinska Alfa",
        "izdajatelj_davcna_stevilka": "15012557",
        "izdajatelj_maticna_stevilka": "1234567000",
        "oznaka_pogodbe": "P-1",
        "status": Invoice.Status.IZBRAN_ZA_POSILJANJE,
        "osnova_skupaj": Decimal("100.00"),
        "ddv_skupaj": Decimal("22.00"),
        "skupaj_z_ddv": Decimal("122.00"),
    }
    return Invoice.objects.create(**(values | changes))


def _line(
    invoice,
    *,
    group="STANDARD",
    rate="22.00",
    net="100.00",
    vat="22.00",
    method="OBDAVCENO",
    clause="",
):
    net_value, vat_value = Decimal(net), Decimal(vat)
    return InvoiceLine.objects.create(
        invoice=invoice,
        opis="Najemnina",
        osnova=net_value,
        ddv_stopnja=Decimal(rate),
        ddv_znesek=vat_value,
        skupaj=net_value + vat_value,
        ddv_nacin_obracuna=method,
        ddv_klavzula=clause,
        ddv_rekap_skupina=group,
    )


def _lease(company, *, reference="P-NE"):
    prop = Property.objects.create(company=company, naziv="Objekt", naslov="Naslov")
    unit = Unit.objects.create(
        property=prop, oznaka=reference, tip_enote=Unit.TipEnote.PISARNA
    )
    tenant = Tenant.objects.create(
        tip=Tenant.Tip.PRAVNA_OSEBA,
        naziv="Zamrznjeni najemnik",
        davcna_stevilka="87654321",
        maticna_stevilka="7654321000",
        naslov="Stari naslov",
        drzava_koda="SI",
        identificiran_za_ddv=True,
        email="tenant@example.com",
    )
    return Lease.objects.create(
        unit=unit,
        tenant=tenant,
        oznaka_pogodbe=reference,
        tip_trajanja=Lease.TipTrajanja.NEDOLOCEN_CAS,
        datum_zacetka=date(2026, 1, 1),
        rok_placila_dni=15,
    )


def _frozen_non_issue(company, *, user_reason=None, system_reason=None, period=date(2026, 9, 1)):
    lease = _lease(company, reference=f"P-{Lease.objects.count() + 1}")
    batch = _ensure_completed_batch(company, period)
    candidate = {
        "lease_id": lease.pk,
        "contract_reference": lease.oznaka_pogodbe,
        "property_label": "Objekt",
        "due_date": "2026-09-20",
        "tenant": {
            "name": "Zamrznjeni najemnik",
            "type": "PRAVNA_OSEBA",
            "address": "Stari naslov",
            "country": "SI",
            "vat_registered": True,
            "tax_number": "87654321",
            "full_deduction": False,
            "vat_id": "SI87654321",
            "registration_number": "7654321000",
        },
        "lines": [] if system_reason else [{
            "lease_item_id": 1,
            "description": "Najemnina",
            "net": "100.00",
            "vat_rate": "22.00",
            "vat": "22.00",
            "total": "122.00",
            "vat_method": "OBDAVCENO",
            "vat_clause": "",
            "vat_summary_group": "STANDARD",
            "proportional": False,
            "vat_regime_code": "OBDAVCENO_STANDARD",
            "requires_full_deduction": False,
        }],
        "totals": (
            {"net": "0.00", "vat": "0.00", "total": "0.00"}
            if system_reason
            else {"net": "100.00", "vat": "22.00", "total": "122.00"}
        ),
        "delivery_contacts": {},
        "pdf_template_version": "1",
        "will_issue": system_reason is None,
        "non_issue_reason": system_reason or "",
    }
    snapshot = batch.calculation_snapshot
    fingerprint = _candidate_fingerprint(
        candidate,
        company=snapshot["company"],
        period_start=snapshot["period_start"],
        period_end=snapshot["period_end"],
        issue_date=snapshot["issue_date"],
    )
    candidate["candidate_fingerprint"] = fingerprint
    snapshot["candidates"].append(candidate)
    batch.save(update_fields=["calculation_snapshot"])
    MonthlyCandidateReview.objects.create(
        batch=batch,
        lease=lease,
        candidate_fingerprint=fingerprint,
        selected=False,
        requires_confirmation=False,
        non_issue_reason=user_reason or "",
    )
    return lease, candidate, batch


@pytest.mark.django_db
def test_historical_schema3_report_preserves_unknown_eligibility(company):
    invoice = _invoice(company)
    _line(invoice)
    _lease_obj, candidate, batch = _frozen_non_issue(company, user_reason="Stari razlog")
    old_fingerprint = candidate.pop("candidate_fingerprint")
    candidate["tenant"].pop("full_deduction")
    for line in candidate["lines"]:
        line.pop("vat_regime_code")
        line.pop("requires_full_deduction")
    snapshot = batch.calculation_snapshot
    snapshot["schema_version"] = 3
    candidate["candidate_fingerprint"] = _candidate_fingerprint(
        candidate, company=snapshot["company"], period_start=snapshot["period_start"],
        period_end=snapshot["period_end"], issue_date=snapshot["issue_date"], schema_version=3,
    )
    batch.calculation_schema_version = 3
    batch.save(update_fields=["calculation_snapshot", "calculation_schema_version"])
    MonthlyCandidateReview.objects.filter(batch=batch, candidate_fingerprint=old_fingerprint).update(
        candidate_fingerprint=candidate["candidate_fingerprint"]
    )
    report = create_accountant_report(company=company, period=date(2026, 9, 1))
    assert report.snapshot["unissued_rows"][0]["reason"] == "Stari razlog"
    invoice.refresh_from_db()
    assert invoice.prejemnik_polni_odbitek is None


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("instant", "expected_timestamp", "expected_date"),
    [
        (
            datetime(2026, 8, 1, 23, 30, tzinfo=UTC),
            "2026-08-02T01:30:00+02:00",
            "02.08.2026",
        ),
        (
            datetime(2026, 1, 15, 23, 30, tzinfo=UTC),
            "2026-01-16T00:30:00+01:00",
            "16.01.2026",
        ),
    ],
)
def test_snapshot_persists_local_creation_offset_and_csv_calendar_date(
    company, monkeypatch, instant, expected_timestamp, expected_date
):
    _line(_invoice(company))
    monkeypatch.setattr("accounting_export.services.timezone.now", lambda: instant)

    report = create_accountant_report(company=company, period=date(2026, 9, 1))
    report.refresh_from_db()

    assert report.snapshot["header"]["generated_at"] == expected_timestamp
    assert report.generated_at == instant
    assert report.schema_version == report.snapshot["schema_version"] == 1
    assert expected_date.encode() in render_accountant_report_csv(report)


@pytest.mark.django_db
def test_snapshot_uses_invoice_snapshots_and_scopes_company_month(company):
    included = _invoice(company)
    _line(included)
    other = Company.objects.create(
        naziv="Druga", davcna_stevilka="26041347", maticna_stevilka="1111111000",
        naslov="Druga 1", drzava_koda="SI", trr="SI56191000000123438",
        logotip="logo.png", email_racunovodja="d@example.com",
        email_posiljatelj="d@example.com", keychain_kljuc_posiljatelja="druga",
    )
    _line(_invoice(other, number="NP-00002"))
    _line(_invoice(company, number="NP-00003", issued=date(2026, 8, 31)))

    report = create_accountant_report(company=company, period=date(2026, 9, 1))

    assert report.period == date(2026, 9, 1)
    assert report.snapshot["header"] | {"generated_at": None} == {
        "company_name": "Zgodovinska Alfa",
        "company_tax_number": "15012557",
        "company_registration_number": "1234567000",
        "period": "2026-09",
        "generated_at": None,
    }
    assert [row["invoice_number"] for row in report.snapshot["issued_rows"]] == ["NP-00001"]
    assert report.snapshot["issued_rows"][0] == {
        "invoice_number": "NP-00001", "document_type": "NP",
        "recipient_name": "Stari najemnik", "recipient_tax_number": "87654321",
        "recipient_registration_number": "7654321000", "contract_reference": "P-1",
        "issue_date": "2026-09-05", "service_period_start": "2026-09-01",
        "service_period_end": "2026-09-30",
        "vat_regimes": [{
            "vat_method": "OBDAVCENO",
            "vat_rate": "22.00",
            "vat_summary_group": "STANDARD",
            "vat_clause": "",
        }],
        "net_amount": "100.00", "vat_amount": "22.00", "gross_amount": "122.00",
    }


@pytest.mark.django_db
def test_issued_row_preserves_unique_historical_vat_regimes(company):
    invoice = _invoice(company)
    _line(
        invoice,
        method="OBDAVCENO",
        rate="22.00",
        group="STANDARD",
        clause="Obdavčeno",
    )
    _line(
        invoice,
        method="OBRNJENA_DAVCNA",
        rate="22.00",
        group="STANDARD",
        clause="Obrnjena davčna obveznost",
    )
    _line(
        invoice,
        method="OBDAVCENO",
        rate="22.00",
        group="STANDARD",
        clause="Obdavčeno",
    )

    report = create_accountant_report(company=company, period=date(2026, 9, 1))

    assert report.snapshot["issued_rows"][0]["vat_regimes"] == [
        {
            "vat_method": "OBDAVCENO",
            "vat_rate": "22.00",
            "vat_summary_group": "STANDARD",
            "vat_clause": "Obdavčeno",
        },
        {
            "vat_method": "OBRNJENA_DAVCNA",
            "vat_rate": "22.00",
            "vat_summary_group": "STANDARD",
            "vat_clause": "Obrnjena davčna obveznost",
        },
    ]


@pytest.mark.django_db
@pytest.mark.parametrize("kind", Invoice.DocumentType.values)
def test_all_numbered_document_types_are_included(company, kind):
    invoice = _invoice(company, kind=kind, number=f"{kind}-1")
    _line(invoice)
    report = create_accountant_report(company=company, period=date(2026, 9, 1))
    assert report.snapshot["issued_rows"][0]["document_type"] == kind


@pytest.mark.django_db
@pytest.mark.parametrize(
    "status", [Invoice.Status.NAPAKA, Invoice.Status.REZULTAT_NI_ZNAN]
)
def test_numbered_delivery_failure_or_unknown_remains_included(company, status):
    invoice = _invoice(company, status=status)
    _line(invoice)
    report = create_accountant_report(company=company, period=date(2026, 9, 1))
    assert [row["invoice_number"] for row in report.snapshot["issued_rows"]] == [
        "NP-00001"
    ]


@pytest.mark.django_db
def test_issued_invoice_without_monthly_batch_is_insufficient(company):
    invoice = _invoice(company, completed_evidence=False)
    _line(invoice)
    with pytest.raises(ValidationError, match="completed monthly evidence"):
        create_accountant_report(company=company, period=date(2026, 9, 1))
    assert not AccountantReport.objects.exists()


@pytest.mark.django_db
def test_step_five_in_progress_cycle_is_insufficient(company):
    invoice = _invoice(company)
    _line(invoice)
    MonthlyCycle.objects.update(
        status=MonthlyCycle.Status.V_TEKU,
        trenutni_korak=MonthlyCycle.Korak.POTRDITEV_PAKETA,
    )
    with pytest.raises(ValidationError, match="completed monthly evidence"):
        create_accountant_report(company=company, period=date(2026, 9, 1))
    assert not AccountantReport.objects.exists()


@pytest.mark.django_db
def test_completed_cycle_and_calculated_batch_allow_report(company):
    invoice = _invoice(company)
    _line(invoice)
    assert create_accountant_report(
        company=company, period=date(2026, 9, 1)
    ).pk


@pytest.mark.django_db
@pytest.mark.parametrize(
    "status",
    [
        MonthlyBatch.CalculationStatus.NOT_CALCULATED,
        MonthlyBatch.CalculationStatus.BLOCKED,
    ],
)
def test_incomplete_or_blocked_batch_is_insufficient(company, status):
    invoice = _invoice(company)
    _line(invoice)
    MonthlyBatch.objects.update(calculation_status=status)
    with pytest.raises(ValidationError, match="completed monthly evidence"):
        create_accountant_report(company=company, period=date(2026, 9, 1))
    assert not AccountantReport.objects.exists()


@pytest.mark.django_db
def test_incomplete_calculation_snapshot_is_rejected(company):
    invoice = _invoice(company)
    _line(invoice)
    MonthlyBatch.objects.update(calculation_snapshot={"candidates": []})
    with pytest.raises(ValidationError, match="zamrznjenega posnetka"):
        create_accountant_report(company=company, period=date(2026, 9, 1))
    assert not AccountantReport.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "invalid_kind",
    ["missing-candidates", "candidates-not-list", "snapshot-not-dict"],
    ids=["missing-candidates", "candidates-not-list", "snapshot-not-dict"],
)
def test_invalid_candidates_collection_is_rejected_without_report(company, invalid_kind):
    invoice = _invoice(company)
    _line(invoice)
    snapshot = _snapshot(company)
    if invalid_kind == "missing-candidates":
        snapshot.pop("candidates")
    elif invalid_kind == "candidates-not-list":
        snapshot["candidates"] = {}
    else:
        snapshot = []
    MonthlyBatch.objects.update(calculation_snapshot=snapshot)
    with pytest.raises(ValidationError, match="[Zz]amrznjen"):
        create_accountant_report(company=company, period=date(2026, 9, 1))
    assert not AccountantReport.objects.exists()


@pytest.mark.django_db
def test_drafts_unnumbered_and_archived_non_issued_are_excluded(company):
    valid = _invoice(company)
    _line(valid)
    _line(_invoice(company, number="NP-DRAFT", status=Invoice.Status.OSNUTEK))
    _line(_invoice(company, number=None, status=Invoice.Status.OSNUTEK))
    _line(_invoice(company, number=None, status=Invoice.Status.ARHIVIRAN_NI_IZDAN))
    report = create_accountant_report(company=company, period=date(2026, 9, 1))
    assert [row["invoice_number"] for row in report.snapshot["issued_rows"]] == ["NP-00001"]


@pytest.mark.django_db
def test_recap_groups_by_group_and_rate(company):
    invoice = _invoice(company)
    _line(invoice, group="A", rate="22.00", net="100.00", vat="22.00")
    _line(invoice, group="A", rate="22.00", net="50.00", vat="11.00")
    _line(invoice, group="A", rate="9.50", net="20.00", vat="1.90")
    _line(invoice, group="B", rate="22.00", net="10.00", vat="2.20")
    report = create_accountant_report(company=company, period=date(2026, 9, 1))
    assert report.snapshot["recap_rows"] == [
        {"vat_summary_group": "A", "vat_rate": "9.50", "net_amount": "20.00", "vat_amount": "1.90", "gross_amount": "21.90"},
        {"vat_summary_group": "A", "vat_rate": "22.00", "net_amount": "150.00", "vat_amount": "33.00", "gross_amount": "183.00"},
        {"vat_summary_group": "B", "vat_rate": "22.00", "net_amount": "10.00", "vat_amount": "2.20", "gross_amount": "12.20"},
    ]


@pytest.mark.django_db
def test_non_taxable_subject_line_stays_in_invoice_but_not_recap(company):
    invoice = _invoice(company)
    _line(invoice, net="100.00", vat="22.00")
    _line(
        invoice,
        method="NI_PREDMET",
        group="NI_PREDMET",
        rate="0.00",
        net="50.00",
        vat="0.00",
    )

    report = create_accountant_report(company=company, period=date(2026, 9, 1))
    row = report.snapshot["issued_rows"][0]

    assert (row["net_amount"], row["vat_amount"], row["gross_amount"]) == (
        "150.00",
        "22.00",
        "172.00",
    )
    assert {regime["vat_method"] for regime in row["vat_regimes"]} == {
        "OBDAVCENO",
        "NI_PREDMET",
    }
    assert report.snapshot["recap_rows"] == [{
        "vat_summary_group": "STANDARD",
        "vat_rate": "22.00",
        "net_amount": "100.00",
        "vat_amount": "22.00",
        "gross_amount": "122.00",
    }]


@pytest.mark.django_db
def test_only_non_taxable_subject_lines_leave_recap_empty(company):
    invoice = _invoice(company)
    _line(
        invoice,
        method="NI_PREDMET",
        group="NI_PREDMET",
        rate="0.00",
        net="50.00",
        vat="0.00",
    )

    report = create_accountant_report(company=company, period=date(2026, 9, 1))
    row = report.snapshot["issued_rows"][0]

    assert (row["net_amount"], row["vat_amount"], row["gross_amount"]) == (
        "50.00",
        "0.00",
        "50.00",
    )
    assert row["vat_regimes"][0]["vat_method"] == "NI_PREDMET"
    assert report.snapshot["recap_rows"] == []


@pytest.mark.django_db
def test_non_issued_rows_use_frozen_user_and_system_reasons(company):
    _line(_invoice(company))
    user_lease, _candidate, _batch = _frozen_non_issue(
        company, user_reason="Dogovorjena neizdaja"
    )
    system_lease, _candidate, _batch = _frozen_non_issue(
        company, system_reason="Ni obračunljivih postavk"
    )
    Lease.objects.filter(pk__in=[user_lease.pk, system_lease.pk]).update(
        oznaka_pogodbe="SPREMENJENO", status=Lease.Status.PRENEHALA
    )
    report = create_accountant_report(company=company, period=date(2026, 9, 1))
    assert [(row["contract_reference"], row["reason"]) for row in report.snapshot["unissued_rows"]] == [
        ("P-1", "Dogovorjena neizdaja"),
        ("P-2", "Ni obračunljivih postavk"),
    ]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("reason_kwargs", "expected_reason", "expected_source"),
    [
        ({"user_reason": "Uporabniški razlog"}, "Uporabniški razlog", "USER"),
        ({"system_reason": "Sistemski razlog"}, "Sistemski razlog", "SYSTEM"),
    ],
)
def test_valid_frozen_non_issue_reason_is_reported(
    company, reason_kwargs, expected_reason, expected_source
):
    _line(_invoice(company))
    _frozen_non_issue(company, **reason_kwargs)
    report = create_accountant_report(company=company, period=date(2026, 9, 1))
    row = report.snapshot["unissued_rows"][0]
    assert (row["reason"], row["reason_source"]) == (
        expected_reason,
        expected_source,
    )


@pytest.mark.django_db
def test_tampered_frozen_candidate_is_rejected(company):
    _line(_invoice(company))
    _lease_value, candidate, batch = _frozen_non_issue(
        company, user_reason="Dogovorjena neizdaja"
    )
    candidate["contract_reference"] = "TAMPERED"
    batch.save(update_fields=["calculation_snapshot"])
    with pytest.raises(ValidationError, match="Prstni odtis"):
        create_accountant_report(company=company, period=date(2026, 9, 1))
    assert not AccountantReport.objects.exists()


@pytest.mark.django_db
def test_mismatched_review_fingerprint_is_rejected(company):
    _line(_invoice(company))
    _lease_value, _candidate, batch = _frozen_non_issue(
        company, user_reason="Dogovorjena neizdaja"
    )
    review = MonthlyCandidateReview.objects.get(batch=batch)
    review.candidate_fingerprint = "f" * 64
    review.save(update_fields=["candidate_fingerprint"])
    with pytest.raises(ValidationError, match="fingerprint"):
        create_accountant_report(company=company, period=date(2026, 9, 1))
    assert not AccountantReport.objects.exists()


@pytest.mark.django_db
def test_report_snapshot_stays_historical_and_instance_operations_are_append_only(company):
    invoice = _invoice(company)
    _line(invoice)
    report = create_accountant_report(company=company, period=date(2026, 9, 1))
    before = report.snapshot
    assert before["issued_rows"][0]["vat_regimes"] == [{
        "vat_method": "OBDAVCENO",
        "vat_rate": "22.00",
        "vat_summary_group": "STANDARD",
        "vat_clause": "",
    }]
    Company.objects.filter(pk=company.pk).update(naziv="Nova Alfa")
    Invoice.objects.filter(pk=invoice.pk).update(prejemnik_naziv="Nov najemnik")
    InvoiceLine.objects.filter(invoice=invoice).update(osnova=Decimal("999.00"))
    report.refresh_from_db()
    assert report.snapshot == before
    report.snapshot = {}
    with pytest.raises(ValidationError, match="cannot be overwritten"):
        report.save()
    with pytest.raises(ValidationError, match="cannot be deleted"):
        report.delete()


@pytest.mark.django_db
def test_live_tenant_lease_item_and_vat_changes_do_not_change_report(company):
    lease = _lease(company)
    regime = VatRegime.objects.create(
        zahteva_polni_odbitek=False,
        sifra="DDV22", naziv="DDV 22", nacin_obracuna="OBDAVCENO",
        stopnja=Decimal("22.00"), rekapitulacijska_skupina="STANDARD",
        velja_od=date(2026, 1, 1),
    )
    item = LeaseItem.objects.create(
        lease=lease, naziv_postavke="Najemnina", znesek_neto_mesecno=Decimal("100.00"),
        vat_regime=regime,
    )
    invoice = _invoice(company, lease=lease)
    line = _line(invoice)
    line.lease = lease
    line.lease_item = item
    line.save(update_fields=["lease", "lease_item"])
    report = create_accountant_report(company=company, period=date(2026, 9, 1))
    before = report.snapshot
    historical_regimes = before["issued_rows"][0]["vat_regimes"]

    Tenant.objects.filter(pk=lease.tenant_id).update(naziv="Nov živi najemnik")
    Lease.objects.filter(pk=lease.pk).update(oznaka_pogodbe="NOVA")
    LeaseItem.objects.filter(pk=item.pk).update(znesek_neto_mesecno=Decimal("999.00"))
    VatRegime.objects.filter(pk=regime.pk).update(
        nacin_obracuna="OPROSCENO",
        stopnja=Decimal("9.50"),
        rekapitulacijska_skupina="DRUGA",
        klavzula="Nova živa klavzula",
    )

    report.refresh_from_db()
    assert report.snapshot == before
    assert report.snapshot["issued_rows"][0]["vat_regimes"] == historical_regimes


@pytest.mark.django_db
def test_unissued_rows_exclude_other_company_and_month(company):
    _line(_invoice(company))
    _frozen_non_issue(company, user_reason="Vključen")
    other = Company.objects.create(
        naziv="Druga", davcna_stevilka="26041347", maticna_stevilka="1111111000",
        naslov="Druga 1", drzava_koda="SI", trr="SI56191000000123438",
        logotip="logo.png", email_racunovodja="d@example.com",
        email_posiljatelj="d@example.com", keychain_kljuc_posiljatelja="druga",
    )
    _frozen_non_issue(other, user_reason="Drugo podjetje")
    _frozen_non_issue(company, user_reason="Drug mesec", period=date(2026, 8, 1))
    report = create_accountant_report(company=company, period=date(2026, 9, 1))
    assert [row["reason"] for row in report.snapshot["unissued_rows"]] == ["Vključen"]


@pytest.mark.django_db
def test_generation_is_deterministic_except_generation_metadata(company):
    _line(_invoice(company))
    first = create_accountant_report(company=company, period=date(2026, 9, 1))
    second = create_accountant_report(company=company, period=date(2026, 9, 1))
    first_snapshot, second_snapshot = first.snapshot, second.snapshot
    first_snapshot["header"].pop("generated_at")
    second_snapshot["header"].pop("generated_at")
    assert first_snapshot == second_snapshot


@pytest.mark.django_db
def test_generation_does_not_mutate_monthly_or_delivery_archive_state(company):
    invoice = _invoice(company)
    _line(invoice)
    _frozen_non_issue(company, user_reason="Ne izdaj")
    cycle = MonthlyCycle.objects.get(obdobje=date(2026, 9, 1))
    cycle.completion_snapshot = {"fixed": True}
    cycle.save(update_fields=["completion_snapshot"])
    before = {
        "invoice": (invoice.status, invoice.stevilka, invoice.osnova_skupaj),
        "cycle": (cycle.status, cycle.trenutni_korak, cycle.completion_snapshot),
        "delivery": DeliveryAttempt.objects.count(),
        "summary": MonthlySummaryAttempt.objects.count(),
        "archive": ArchivedFile.objects.count(),
    }
    create_accountant_report(company=company, period=date(2026, 9, 1))
    invoice.refresh_from_db()
    cycle.refresh_from_db()
    assert {
        "invoice": (invoice.status, invoice.stevilka, invoice.osnova_skupaj),
        "cycle": (cycle.status, cycle.trenutni_korak, cycle.completion_snapshot),
        "delivery": DeliveryAttempt.objects.count(),
        "summary": MonthlySummaryAttempt.objects.count(),
        "archive": ArchivedFile.objects.count(),
    } == before


@pytest.mark.django_db
def test_conflicting_issuer_snapshots_reject_without_partial_report(company):
    _line(_invoice(company, number="NP-1"))
    _line(_invoice(company, number="ST-1", kind="ST", izdajatelj_naziv="Druga zgodovina"))
    with pytest.raises(ValidationError, match="issuer identity snapshots conflict"):
        create_accountant_report(company=company, period=date(2026, 9, 1))
    assert not AccountantReport.objects.exists()


@pytest.mark.django_db
def test_no_issued_invoices_rejects_without_live_company_fallback(company):
    _frozen_non_issue(company, user_reason="Ne izdaj")
    with pytest.raises(ValidationError, match="no issued invoice snapshot"):
        create_accountant_report(company=company, period=date(2026, 9, 1))
    assert not AccountantReport.objects.exists()
