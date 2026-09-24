import base64
from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.utils import timezone

from catalogs.models import VatRegime
from companies.models import Company, NumberSequence, Signatory
from companies.services import potrdi_ddv_nastavitve, potrdi_vzorec_podjetja
from contracts.models import Lease, LeaseItem, Property, RecipientContact, Tenant, Unit
from invoicing.models import (
    Invoice,
    InvoiceLine,
    MonthlyBatch,
    MonthlyCandidateReview,
    MonthlyCycle,
)
from invoicing.services import (
    CALCULATION_SNAPSHOT_VERSION,
    MIGRACIJSKI_RAZLOG,
    NeveljavenPrehodMesecnegaCikla,
    _candidate_fingerprint,
    izdaj_zamrznjeni_mesecni_paket,
    izracunaj_mesecne_kandidate,
    materializiraj_zamrznjenega_mesecnega_kandidata,
    nastavi_izbiro_kandidata,
    potrdi_izjemo_kandidata,
    preveri_predhodno_kontrolo,
    stanje_zamrznjenega_mesecnega_kandidata,
    trenutni_pregledi_mesecnega_cikla,
    uskladi_preglede_mesecnega_cikla,
    zamrzni_paket_za_potrditev,
)
from rendering.contexts import pripravi_mesecni_pdf_context

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg=="
)


@pytest.fixture
def ready_company(db, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    (tmp_path / "logo.png").write_bytes(PNG)
    (tmp_path / "signature.png").write_bytes(PNG)
    company = Company.objects.create(
        naziv="Alfa d.o.o.",
        davcna_stevilka="15012557",
        maticna_stevilka="1234567000",
        naslov="Glavna 1",
        drzava_koda="SI",
        trr="SI56191000000123438",
        logotip="logo.png",
        ddv_zavezanec=True,
        email_racunovodja="racunovodja@example.com",
        email_posiljatelj="racuni@example.com",
        keychain_kljuc_posiljatelja="alfa",
        ddv_razporeditev_potrjena=True,
    )
    signatory = Signatory.objects.create(
        company=company,
        ime_priimek="Ana Podpisnica",
        funkcija="Direktorica",
        podpis_slika="signature.png",
        velja_od=date(2026, 1, 1),
    )
    property_ = Property.objects.create(
        company=company, naziv="Stavba A", naslov="Glavna 1"
    )
    unit = Unit.objects.create(
        property=property_, oznaka="P-1", tip_enote=Unit.TipEnote.PISARNA
    )
    tenant = Tenant.objects.create(
        tip=Tenant.Tip.PRAVNA_OSEBA,
        naziv="Najemnik d.o.o.",
        davcna_stevilka="15012557",
        maticna_stevilka="1234567000",
        naslov="Trg 1",
        drzava_koda="SI",
        identificiran_za_ddv=True,
        email="najemnik@example.com",
    )
    lease = Lease.objects.create(
        unit=unit,
        tenant=tenant,
        oznaka_pogodbe="P-2026",
        tip_trajanja=Lease.TipTrajanja.NEDOLOCEN_CAS,
        datum_zacetka=date(2026, 1, 1),
        rok_placila_dni=15,
    )
    regime = VatRegime.objects.create(
        sifra="R22",
        naziv="DDV 22",
        nacin_obracuna=VatRegime.NacinObracuna.OBDAVCENO,
        stopnja=Decimal("22.00"),
        rekapitulacijska_skupina="DDV 22 %",
        eslog_koda="S",
        velja_od=date(2026, 1, 1),
    )
    item = LeaseItem.objects.create(
        lease=lease,
        naziv_postavke="Najemnina",
        znesek_neto_mesecno=Decimal("100.00"),
        vat_regime=regime,
    )
    potrdi_ddv_nastavitve(company)
    potrdi_vzorec_podjetja(company, lease)
    cycle = MonthlyCycle.objects.create(
        obdobje=date(2026, 9, 1),
        datum_izdaje=date(2026, 9, 10),
        trenutni_korak=MonthlyCycle.Korak.PREDHODNA_KONTROLA,
    )
    batch = MonthlyBatch.objects.create(cycle=cycle, company=company)
    return {
        "company": company,
        "signatory": signatory,
        "property": property_,
        "unit": unit,
        "tenant": tenant,
        "lease": lease,
        "regime": regime,
        "item": item,
        "cycle": cycle,
        "batch": batch,
    }


def _codes(batch):
    batch.refresh_from_db()
    return {issue["code"] for issue in batch.preflight_issues}


def _add_ready_company(data):
    company = Company.objects.create(
        naziv="Beta d.o.o.", davcna_stevilka="12345679",
        maticna_stevilka="1234567001", naslov="Druga 2", drzava_koda="SI",
        trr="SI56191000000123438", logotip="logo.png",
        email_racunovodja="beta@example.com", email_posiljatelj="beta@example.com",
        keychain_kljuc_posiljatelja="beta", ddv_razporeditev_potrjena=True,
    )
    Signatory.objects.create(
        company=company, ime_priimek="Berta Podpisnica",
        podpis_slika="signature.png", velja_od=date(2026, 1, 1),
    )
    property_ = Property.objects.create(company=company, naziv="Stavba B", naslov="Druga 2")
    unit = Unit.objects.create(property=property_, oznaka="B-1", tip_enote=Unit.TipEnote.PISARNA)
    tenant = Tenant.objects.create(
        tip=Tenant.Tip.PRAVNA_OSEBA, naziv="Beta najemnik",
        davcna_stevilka="12345679", maticna_stevilka="1234567001",
        naslov="Trg 2", drzava_koda="SI", identificiran_za_ddv=False,
        email="beta@example.com",
    )
    lease = Lease.objects.create(
        unit=unit, tenant=tenant, oznaka_pogodbe="B-2026",
        tip_trajanja=Lease.TipTrajanja.NEDOLOCEN_CAS,
        datum_zacetka=date(2026, 1, 1), rok_placila_dni=15,
    )
    LeaseItem.objects.create(
        lease=lease, znesek_neto_mesecno=Decimal("80.00"),
        vat_regime=data["regime"],
    )
    potrdi_ddv_nastavitve(company)
    potrdi_vzorec_podjetja(company, lease)
    return company, MonthlyBatch.objects.create(cycle=data["cycle"], company=company)


def _add_lease(data, reference="P-2", *, with_item=False, archived=False):
    lease = Lease.objects.create(
        unit=data["unit"], tenant=data["tenant"], oznaka_pogodbe=reference,
        tip_trajanja=Lease.TipTrajanja.NEDOLOCEN_CAS,
        datum_zacetka=date(2026, 1, 1), rok_placila_dni=15,
        arhivirana=archived,
    )
    if with_item:
        LeaseItem.objects.create(
            lease=lease, naziv_postavke="Najemnina",
            znesek_neto_mesecno=Decimal("50.00"), vat_regime=data["regime"],
        )
    return lease


def _historical_invoice(data, lease, *, status=Invoice.Status.POSLAN, reason=None):
    return Invoice.objects.create(
        company=data["company"], lease=lease, document_type=Invoice.DocumentType.NP,
        status=status, stevilka="NP-OLD" if status == Invoice.Status.POSLAN else None,
        datum_izdaje=date(2026, 8, 10), obdobje_od=date(2026, 8, 1),
        obdobje_do=date(2026, 8, 31), skupaj_z_ddv=Decimal("55.00"),
        razlog_arhiviranja=reason,
        prejemnik_tip=data["tenant"].tip, prejemnik_naziv=data["tenant"].naziv,
        prejemnik_naslov=data["tenant"].naslov,
        prejemnik_drzava_koda=data["tenant"].drzava_koda,
        prejemnik_identificiran_za_ddv=data["tenant"].identificiran_za_ddv,
        prejemnik_davcna_stevilka=data["tenant"].davcna_stevilka,
        prejemnik_ddv_identifikacijska_stevilka=(
            data["tenant"].ddv_identifikacijska_stevilka
        ),
        prejemnik_maticna_stevilka=data["tenant"].maticna_stevilka,
        izdajatelj_naziv=data["company"].naziv,
        izdajatelj_naslov=data["company"].naslov,
        izdajatelj_drzava_koda=data["company"].drzava_koda,
        izdajatelj_identificiran_za_ddv=data["company"].ddv_zavezanec,
        izdajatelj_davcna_stevilka=data["company"].davcna_stevilka,
        izdajatelj_maticna_stevilka=data["company"].maticna_stevilka,
        izdajatelj_trr=data["company"].trr,
        oznaka_pogodbe=lease.oznaka_pogodbe,
        objekt_naziv=lease.unit.property.naziv,
    )


def _calculate(data):
    preveri_predhodno_kontrolo(data["cycle"])
    data["cycle"].trenutni_korak = MonthlyCycle.Korak.IZRACUN_OSNUTKOV
    data["cycle"].save(update_fields=["trenutni_korak"])
    izracunaj_mesecne_kandidate(data["cycle"])
    data["batch"].refresh_from_db()
    return data["batch"].calculation_snapshot["candidates"]


def _candidate_codes(candidate):
    return {item["code"] for item in candidate["exceptions"]}


def test_mesecni_pdf_context_se_po_spremembah_zivih_podatkov_ne_spremeni(
    ready_company,
):
    data = ready_company
    candidate = _calculate(data)[0]
    snapshot = data["batch"].calculation_snapshot
    before = pripravi_mesecni_pdf_context(snapshot, candidate)

    Company.objects.filter(pk=data["company"].pk).update(naziv="Novo podjetje")
    Tenant.objects.filter(pk=data["tenant"].pk).update(naziv="Nov najemnik")
    Lease.objects.filter(pk=data["lease"].pk).update(oznaka_pogodbe="NOVA")
    LeaseItem.objects.filter(pk=data["item"].pk).update(
        naziv_postavke="Nova postavka", znesek_neto_mesecno=Decimal("999.00")
    )
    VatRegime.objects.filter(pk=data["regime"].pk).update(stopnja=Decimal("9.50"))

    assert pripravi_mesecni_pdf_context(snapshot, candidate) == before


def _enter_step4_with_reviews(data, *, will_issue=True, exceptions=None):
    _calculate(data)
    data["batch"].refresh_from_db()
    snapshot = data["batch"].calculation_snapshot
    candidate = snapshot["candidates"][0]
    candidate["will_issue"] = will_issue
    candidate["exceptions"] = [] if exceptions is None else exceptions
    data["batch"].calculation_snapshot = snapshot
    data["batch"].save(update_fields=["calculation_snapshot"])
    data["cycle"].trenutni_korak = MonthlyCycle.Korak.PAKETNI_PREGLED
    data["cycle"].save(update_fields=["trenutni_korak"])
    uskladi_preglede_mesecnega_cikla(data["cycle"])
    return candidate, MonthlyCandidateReview.objects.get()


def _freeze_ready_package(data):
    candidate = _calculate(data)[0]
    data["cycle"].trenutni_korak = MonthlyCycle.Korak.PAKETNI_PREGLED
    data["cycle"].save(update_fields=["trenutni_korak"])
    uskladi_preglede_mesecnega_cikla(data["cycle"])
    review = MonthlyCandidateReview.objects.get()
    if review.requires_confirmation:
        potrdi_izjemo_kandidata(
            data["cycle"], lease_id=review.lease_id,
            candidate_fingerprint=review.candidate_fingerprint,
        )
    zamrzni_paket_za_potrditev(data["cycle"])
    return candidate, review


def _freeze_all_candidates(data):
    candidates = _calculate(data)
    data["cycle"].trenutni_korak = MonthlyCycle.Korak.PAKETNI_PREGLED
    data["cycle"].save(update_fields=["trenutni_korak"])
    uskladi_preglede_mesecnega_cikla(data["cycle"])
    for review in MonthlyCandidateReview.objects.filter(selected=True):
        if review.requires_confirmation:
            potrdi_izjemo_kandidata(
                data["cycle"], lease_id=review.lease_id,
                candidate_fingerprint=review.candidate_fingerprint,
            )
    zamrzni_paket_za_potrditev(data["cycle"])
    return candidates, list(MonthlyCandidateReview.objects.order_by("lease_id"))


def test_batch_defaults_to_not_checked(db):
    assert MonthlyBatch._meta.get_field("preflight_status").get_default() == "NOT_CHECKED"
    assert MonthlyBatch._meta.get_field("preflight_issues").get_default() == []
    assert MonthlyBatch._meta.get_field("calculation_status").get_default() == "NOT_CALCULATED"
    assert MonthlyBatch._meta.get_field("calculation_snapshot").get_default() == {}


def test_calculation_snapshot_default_is_isolated(db):
    field = MonthlyBatch._meta.get_field("calculation_snapshot")
    first = field.get_default()
    first["changed"] = True
    assert field.get_default() == {}


def test_monthly_calculation_persists_deterministic_snapshot_without_side_effects(
    ready_company, settings
):
    data = ready_company
    settings.FEATURE_ESLOG = False
    data["regime"].eslog_koda = ""
    data["regime"].save(update_fields=["eslog_koda"])
    preveri_predhodno_kontrolo(data["cycle"])
    data["cycle"].trenutni_korak = MonthlyCycle.Korak.IZRACUN_OSNUTKOV
    data["cycle"].save(update_fields=["trenutni_korak"])

    before = (Invoice.objects.count(), InvoiceLine.objects.count(), NumberSequence.objects.count())
    izracunaj_mesecne_kandidate(data["cycle"])
    data["batch"].refresh_from_db()
    first_snapshot = data["batch"].calculation_snapshot
    first_time = data["batch"].calculated_at
    izracunaj_mesecne_kandidate(data["cycle"])
    data["batch"].refresh_from_db()

    assert data["batch"].calculation_status == MonthlyBatch.CalculationStatus.CALCULATED
    assert data["batch"].calculation_snapshot == first_snapshot
    assert data["batch"].calculated_at > first_time
    assert data["batch"].calculation_snapshot["candidates"][0]["totals"]["total"] == "122.00"
    assert "ddv_eslog_koda" not in str(data["batch"].calculation_snapshot)
    assert (Invoice.objects.count(), InvoiceLine.objects.count(), NumberSequence.objects.count()) == before


def test_step3_revalidation_blocks_without_overwriting_step2_result(ready_company):
    data = ready_company
    preveri_predhodno_kontrolo(data["cycle"])
    data["cycle"].trenutni_korak = MonthlyCycle.Korak.IZRACUN_OSNUTKOV
    data["cycle"].save(update_fields=["trenutni_korak"])
    Company.objects.filter(pk=data["company"].pk).update(trr="SI00NEVELJAVEN")

    izracunaj_mesecne_kandidate(data["cycle"])
    data["batch"].refresh_from_db()

    assert data["batch"].preflight_status == MonthlyBatch.PreflightStatus.PASSED
    assert data["batch"].calculation_status == MonthlyBatch.CalculationStatus.BLOCKED
    assert data["batch"].calculation_snapshot["blockers"]
    assert data["batch"].calculation_snapshot["candidates"] == []


def test_revalidation_isolates_blocked_company_from_valid_company(ready_company):
    data = ready_company
    _, valid_batch = _add_ready_company(data)
    preveri_predhodno_kontrolo(data["cycle"])
    data["cycle"].trenutni_korak = MonthlyCycle.Korak.IZRACUN_OSNUTKOV
    data["cycle"].save(update_fields=["trenutni_korak"])
    Company.objects.filter(pk=data["company"].pk).update(trr="SI00NEVELJAVEN")

    izracunaj_mesecne_kandidate(data["cycle"])
    data["batch"].refresh_from_db()
    valid_batch.refresh_from_db()

    assert data["batch"].calculation_status == MonthlyBatch.CalculationStatus.BLOCKED
    assert valid_batch.calculation_status == MonthlyBatch.CalculationStatus.CALCULATED


def test_source_change_requires_explicit_rerun_and_replaces_snapshot(ready_company):
    data = ready_company
    preveri_predhodno_kontrolo(data["cycle"])
    data["cycle"].trenutni_korak = MonthlyCycle.Korak.IZRACUN_OSNUTKOV
    data["cycle"].save(update_fields=["trenutni_korak"])
    izracunaj_mesecne_kandidate(data["cycle"])
    data["batch"].refresh_from_db()
    original = data["batch"].calculation_snapshot
    Tenant.objects.filter(pk=data["tenant"].pk).update(email="novo@example.com")
    assert MonthlyBatch.objects.get(pk=data["batch"].pk).calculation_snapshot == original

    izracunaj_mesecne_kandidate(data["cycle"])
    data["batch"].refresh_from_db()

    assert data["batch"].calculation_snapshot != original
    assert data["batch"].calculation_snapshot["candidates"][0]["tenant"]["email"] == "novo@example.com"


def test_candidates_are_ordered_and_archived_lease_is_excluded(ready_company):
    data = ready_company
    second = _add_lease(data, "P-2", with_item=True)
    archived = _add_lease(data, "P-3", with_item=True, archived=True)
    potrdi_ddv_nastavitve(data["company"])
    potrdi_vzorec_podjetja(data["company"], data["lease"])
    preveri_predhodno_kontrolo(data["cycle"])
    data["cycle"].trenutni_korak = MonthlyCycle.Korak.IZRACUN_OSNUTKOV
    data["cycle"].save(update_fields=["trenutni_korak"])

    izracunaj_mesecne_kandidate(data["cycle"])
    data["batch"].refresh_from_db()
    ids = [item["lease_id"] for item in data["batch"].calculation_snapshot["candidates"]]

    assert ids == [data["lease"].pk, second.pk]
    assert archived.pk not in ids


def test_no_lines_candidate_keeps_previous_invoice_and_contacts(ready_company):
    data = ready_company
    lease = _add_lease(data, "P-BREZ")
    previous = _historical_invoice(data, lease)
    contact = RecipientContact.objects.create(
        lease=lease, email="racuni@example.com",
        vloga=RecipientContact.Vloga.TO, kanal=RecipientContact.Kanal.PDF,
    )
    preveri_predhodno_kontrolo(data["cycle"])
    data["cycle"].trenutni_korak = MonthlyCycle.Korak.IZRACUN_OSNUTKOV
    data["cycle"].save(update_fields=["trenutni_korak"])

    izracunaj_mesecne_kandidate(data["cycle"])
    data["batch"].refresh_from_db()
    candidate = next(
        item for item in data["batch"].calculation_snapshot["candidates"]
        if item["lease_id"] == lease.pk
    )

    assert candidate["will_issue"] is False
    assert candidate["previous_invoice"]["id"] == previous.pk
    assert candidate["previous_invoice"]["issuer"]["iban"] == data["company"].trr
    assert candidate["previous_invoice"]["recipient"]["type"] == data["tenant"].tip
    assert candidate["first_issue"] is False
    assert candidate["tenant"]["email"] == data["tenant"].email
    assert candidate["recipient_contacts"] == [{
        "id": contact.pk, "email": contact.email, "role": contact.vloga,
        "channel": contact.kanal,
    }]


@pytest.mark.parametrize(
    ("reason", "expected"),
    [("Uporabnik ni izdal računa", True), (MIGRACIJSKI_RAZLOG, False), (None, False)],
)
def test_previous_month_archived_not_issued_evidence(ready_company, reason, expected):
    data = ready_company
    lease = _add_lease(data, "P-BREZ")
    if reason is not None:
        _historical_invoice(
            data, lease, status=Invoice.Status.ARHIVIRAN_NI_IZDAN, reason=reason
        )
    preveri_predhodno_kontrolo(data["cycle"])
    data["cycle"].trenutni_korak = MonthlyCycle.Korak.IZRACUN_OSNUTKOV
    data["cycle"].save(update_fields=["trenutni_korak"])

    izracunaj_mesecne_kandidate(data["cycle"])
    data["batch"].refresh_from_db()
    candidate = next(
        item for item in data["batch"].calculation_snapshot["candidates"]
        if item["lease_id"] == lease.pk
    )
    evidence = candidate["previous_month_archived_not_issued"]

    assert evidence["exists"] is expected
    assert (evidence["invoice_id"] is not None) is expected
    assert (
        "PREJSNJI_MESEC_ARHIVIRAN_NI_IZDAN" in _candidate_codes(candidate)
    ) is expected


def test_snapshot_v3_fingerprint_and_pdf_contract(ready_company, settings):
    candidate = _calculate(ready_company)[0]

    assert CALCULATION_SNAPSHOT_VERSION == 3
    assert ready_company["batch"].calculation_schema_version == 3
    assert ready_company["batch"].calculation_snapshot["schema_version"] == 3
    assert candidate["pdf_template_version"] == settings.PDF_TEMPLATE_VERSION
    assert len(candidate["candidate_fingerprint"]) == 64
    assert candidate["lease_state"] == {
        "type": Lease.TipTrajanja.NEDOLOCEN_CAS,
        "status": Lease.Status.AKTIVNA,
        "start_date": "2026-01-01",
        "end_date": None,
        "cancellation_date": None,
        "termination_date": None,
        "automatic_extension": False,
        "automatic_extension_months": None,
    }


def test_fingerprint_is_stable_and_excludes_calculation_timestamp(ready_company):
    first = _calculate(ready_company)[0]["candidate_fingerprint"]
    first_time = ready_company["batch"].calculated_at
    izracunaj_mesecne_kandidate(ready_company["cycle"])
    ready_company["batch"].refresh_from_db()

    assert ready_company["batch"].calculated_at > first_time
    assert ready_company["batch"].calculation_snapshot["candidates"][0][
        "candidate_fingerprint"
    ] == first


@pytest.mark.parametrize(
    ("model", "field", "value"),
    [
        (LeaseItem, "znesek_neto_mesecno", Decimal("101.00")),
        (Tenant, "naziv", "Nov prejemnik d.o.o."),
        (Tenant, "email", "nov@example.com"),
    ],
)
def test_document_input_change_changes_fingerprint(
    ready_company, model, field, value
):
    first = _calculate(ready_company)[0]["candidate_fingerprint"]
    model.objects.filter(
        pk=(ready_company["item"].pk if model is LeaseItem else ready_company["tenant"].pk)
    ).update(**{field: value})
    potrdi_vzorec_podjetja(ready_company["company"], ready_company["lease"])
    second = _calculate(ready_company)[0]["candidate_fingerprint"]

    assert second != first


def test_issuer_change_changes_fingerprint(ready_company):
    first = _calculate(ready_company)[0]["candidate_fingerprint"]
    Company.objects.filter(pk=ready_company["company"].pk).update(naziv="Nova Alfa d.o.o.")
    potrdi_vzorec_podjetja(ready_company["company"], ready_company["lease"])
    second = _calculate(ready_company)[0]["candidate_fingerprint"]

    assert second != first


def test_other_candidate_change_does_not_change_fingerprint(ready_company):
    second_lease = _add_lease(ready_company, with_item=True)
    second_item = LeaseItem.objects.get(lease=second_lease)
    potrdi_ddv_nastavitve(ready_company["company"])
    potrdi_vzorec_podjetja(ready_company["company"], ready_company["lease"])
    first_candidates = _calculate(ready_company)
    untouched = next(
        item for item in first_candidates if item["lease_id"] == ready_company["lease"].pk
    )["candidate_fingerprint"]
    LeaseItem.objects.filter(pk=second_item.pk).update(znesek_neto_mesecno=Decimal("51.00"))
    potrdi_vzorec_podjetja(ready_company["company"], ready_company["lease"])
    second_candidates = _calculate(ready_company)

    assert next(
        item for item in second_candidates if item["lease_id"] == ready_company["lease"].pk
    )["candidate_fingerprint"] == untouched


def test_first_issue_and_proportional_exceptions(ready_company):
    Lease.objects.filter(pk=ready_company["lease"].pk).update(
        datum_zacetka=date(2026, 9, 10)
    )
    potrdi_vzorec_podjetja(ready_company["company"], ready_company["lease"])
    codes = _candidate_codes(_calculate(ready_company)[0])

    assert {"PRVA_IZDAJA", "SORAZMERNO"} <= codes


def test_amount_master_data_and_vat_change_exceptions(ready_company):
    previous = _historical_invoice(ready_company, ready_company["lease"])
    previous.izdajatelj_naziv = "Stara Alfa d.o.o."
    previous.prejemnik_naziv = "Star najemnik d.o.o."
    previous.save(update_fields=["izdajatelj_naziv", "prejemnik_naziv"])
    InvoiceLine.objects.create(
        invoice=previous,
        lease=ready_company["lease"],
        lease_item=ready_company["item"],
        opis="Najemnina",
        osnova=Decimal("45.08"),
        ddv_stopnja=Decimal("9.50"),
        ddv_znesek=Decimal("4.28"),
        skupaj=Decimal("49.36"),
        ddv_nacin_obracuna="OBDAVCENO",
        ddv_klavzula="Stara klavzula",
        ddv_rekap_skupina="DDV 9,5 %",
    )
    codes = _candidate_codes(_calculate(ready_company)[0])

    assert {
        "ZNESEK_SPREMENJEN",
        "SPREMEMBA_IZDAJATELJA_NAZIV",
        "SPREMEMBA_PREJEMNIKA_NAZIV",
        "SPREMEMBA_DDV",
    } <= codes


@pytest.mark.parametrize(
    ("updates", "expected"),
    [
        (
            {"tip_trajanja": Lease.TipTrajanja.DOLOCEN_CAS,
             "datum_konca": date(2026, 9, 30)},
            "POGODBA_POTECE_V_OBDOBJU",
        ),
        (
            {"tip_trajanja": Lease.TipTrajanja.DOLOCEN_CAS,
             "datum_konca": date(2026, 10, 15)},
            "POGODBA_POTECE_V_30_DNEH",
        ),
        (
            {"tip_trajanja": Lease.TipTrajanja.DOLOCEN_CAS,
             "datum_konca": date(2026, 9, 30), "samodejno_podaljsanje": True,
             "obdobje_podaljsanja_mesecev": 12},
            "SAMODEJNO_PODALJSANJE_OB_KONCU",
        ),
        (
            {"status": Lease.Status.ODPOVEDANA,
             "datum_odpovedi": date(2026, 8, 1),
             "datum_prenehanja": date(2026, 9, 20)},
            "ODPOVED_ZADNJI_MESEC",
        ),
        (
            {"status": Lease.Status.ODPOVEDANA,
             "datum_odpovedi": date(2026, 8, 1),
             "datum_prenehanja": date(2026, 10, 20)},
            "ODPOVED_PREDZADNJI_MESEC",
        ),
    ],
)
def test_contract_lifecycle_exceptions(ready_company, updates, expected):
    Lease.objects.filter(pk=ready_company["lease"].pk).update(**updates)
    potrdi_vzorec_podjetja(ready_company["company"], ready_company["lease"])

    assert expected in _candidate_codes(_calculate(ready_company)[0])


def test_historical_delivery_contacts_are_compared_only_when_known(ready_company):
    previous = _historical_invoice(ready_company, ready_company["lease"])
    candidate = _calculate(ready_company)[0]
    assert candidate["historical_delivery_contacts_available"] is False
    assert "SPREMEMBA_DOSTAVNIH_KONTAKTOV" not in _candidate_codes(candidate)

    previous.dostavni_kontakti_snapshot = {
        "tenant_email": "stari@example.com", "to": [], "cc": [], "bcc": []
    }
    previous.save(update_fields=["dostavni_kontakti_snapshot"])
    candidate = _calculate(ready_company)[0]
    assert candidate["historical_delivery_contacts_available"] is True
    assert "SPREMEMBA_DOSTAVNIH_KONTAKTOV" in _candidate_codes(candidate)


def test_step4_partial_calculation_upgrades_old_schema(ready_company):
    data = ready_company
    preveri_predhodno_kontrolo(data["cycle"])
    data["cycle"].trenutni_korak = MonthlyCycle.Korak.PAKETNI_PREGLED
    data["cycle"].save(update_fields=["trenutni_korak"])
    MonthlyBatch.objects.filter(pk=data["batch"].pk).update(
        calculation_status=MonthlyBatch.CalculationStatus.CALCULATED,
        calculation_schema_version=2,
        calculation_snapshot={"schema_version": 2},
    )

    result = izracunaj_mesecne_kandidate(
        data["cycle"], samo_brez_aktivnega_izracuna=True
    )
    data["batch"].refresh_from_db()

    assert [batch.pk for batch in result] == [data["batch"].pk]
    assert data["cycle"].trenutni_korak == MonthlyCycle.Korak.PAKETNI_PREGLED
    assert data["batch"].calculation_schema_version == 3
    assert len(data["batch"].calculation_snapshot["candidates"][0][
        "candidate_fingerprint"
    ]) == 64


def test_migration_baseline_only_compares_economics_and_vat(ready_company):
    data = ready_company
    baseline = _historical_invoice(
        data,
        data["lease"],
        status=Invoice.Status.ARHIVIRAN_NI_IZDAN,
        reason=MIGRACIJSKI_RAZLOG,
    )
    baseline.dostavni_kontakti_snapshot = {
        "tenant_email": "zgodovina@example.com",
        "accountant_bcc": "zgodovina-racunovodja@example.com",
        "to": [], "cc": [], "bcc": [],
    }
    baseline.save(update_fields=["dostavni_kontakti_snapshot"])
    InvoiceLine.objects.create(
        invoice=baseline,
        lease=data["lease"],
        lease_item=data["item"],
        opis="Migracijsko izhodišče",
        osnova=Decimal("45.08"),
        ddv_stopnja=Decimal("9.50"),
        ddv_znesek=Decimal("4.28"),
        skupaj=Decimal("49.36"),
        ddv_nacin_obracuna="OBDAVCENO",
        ddv_klavzula="Stara klavzula",
        ddv_rekap_skupina="DDV 9,5 %",
    )

    candidate = _calculate(data)[0]
    codes = _candidate_codes(candidate)

    assert candidate["first_issue"] is False
    assert candidate["previous_invoice"]["is_migration_baseline"] is True
    assert candidate["historical_delivery_contacts_available"] is False
    assert {"ZNESEK_SPREMENJEN", "SPREMEMBA_DDV"} <= codes
    assert not any(
        code.startswith(("SPREMEMBA_IZDAJATELJA_", "SPREMEMBA_PREJEMNIKA_"))
        for code in codes
    )
    assert not {
        "SPREMEMBA_SKLICA_POGODBE",
        "SPREMEMBA_OZNAKE_OBJEKTA",
        "SPREMEMBA_DOSTAVNIH_KONTAKTOV",
    } & codes
    amount = next(
        item for item in candidate["exceptions"]
        if item["code"] == "ZNESEK_SPREMENJEN"
    )
    assert "migracijskega izhodišča" in amount["message"]


def test_vat_only_change_emits_one_authoritative_exception(ready_company):
    data = ready_company
    previous = _historical_invoice(data, data["lease"])
    previous.skupaj_z_ddv = Decimal("122.00")
    previous.save(update_fields=["skupaj_z_ddv"])
    InvoiceLine.objects.create(
        invoice=previous,
        lease=data["lease"],
        lease_item=data["item"],
        opis="Najemnina",
        osnova=Decimal("100.00"),
        ddv_stopnja=Decimal("22.00"),
        ddv_znesek=Decimal("22.00"),
        skupaj=Decimal("122.00"),
        ddv_nacin_obracuna="OBDAVCENO",
        ddv_klavzula="Druga klavzula",
        ddv_rekap_skupina="Druga skupina",
    )

    vat = [
        item for item in _calculate(data)[0]["exceptions"]
        if item["code"] == "SPREMEMBA_DDV"
    ]

    assert len(vat) == 1
    assert vat[0]["lease_item_id"] == data["item"].pk


def test_delivery_snapshot_includes_accountant_and_fingerprints_it(ready_company):
    data = ready_company
    first = _calculate(data)[0]

    assert first["delivery_contacts"]["accountant_bcc"] == data[
        "company"
    ].email_racunovodja

    Company.objects.filter(pk=data["company"].pk).update(
        email_racunovodja="novo-racunovodstvo@example.com"
    )
    second = _calculate(data)[0]

    assert second["delivery_contacts"]["accountant_bcc"] == (
        "novo-racunovodstvo@example.com"
    )
    assert second["candidate_fingerprint"] != first["candidate_fingerprint"]


def test_header_change_messages_use_slovenian_labels(ready_company):
    data = ready_company
    previous = _historical_invoice(data, data["lease"])
    previous.izdajatelj_identificiran_za_ddv = False
    previous.prejemnik_maticna_stevilka = "0000000000"
    previous.save(update_fields=[
        "izdajatelj_identificiran_za_ddv",
        "prejemnik_maticna_stevilka",
    ])

    messages = {
        item["code"]: item["message"] for item in _calculate(data)[0]["exceptions"]
    }

    assert messages["SPREMEMBA_IZDAJATELJA_DDV_STATUS"] == (
        "Spremenjen DDV status izdajatelja."
    )
    assert messages["SPREMEMBA_PREJEMNIKA_MATICNA_STEVILKA"] == (
        "Spremenjena matična številka prejemnika."
    )


def test_step3_get_uses_persisted_snapshot_without_writes(ready_company, client):
    data = ready_company
    preveri_predhodno_kontrolo(data["cycle"])
    data["cycle"].trenutni_korak = MonthlyCycle.Korak.IZRACUN_OSNUTKOV
    data["cycle"].save(update_fields=["trenutni_korak"])
    izracunaj_mesecne_kandidate(data["cycle"])
    data["batch"].refresh_from_db()
    snapshot = data["batch"].calculation_snapshot
    calculated_at = data["batch"].calculated_at
    LeaseItem.objects.filter(pk=data["item"].pk).update(znesek_neto_mesecno=Decimal("999.00"))

    from django.test import Client

    content = Client().get("/mesecni-obracun/").content.decode()
    data["batch"].refresh_from_db()
    assert "122.00" in content or "122,00" in content
    assert data["batch"].calculation_snapshot == snapshot
    assert data["batch"].calculated_at == calculated_at


def test_step4_allows_previously_blocked_company_to_join(ready_company, client):
    data = ready_company
    _, valid_batch = _add_ready_company(data)
    original_iban = data["company"].trr
    Company.objects.filter(pk=data["company"].pk).update(trr="SI00NEVELJAVEN")
    preveri_predhodno_kontrolo(data["cycle"])
    client.post("/mesecni-obracun/", {"action": "continue_step3"})
    client.post("/mesecni-obracun/", {"action": "continue_step4"})
    assert MonthlyCycle.objects.get().trenutni_korak == MonthlyCycle.Korak.PAKETNI_PREGLED
    assert valid_batch.__class__.objects.get(pk=valid_batch.pk).calculation_status == "CALCULATED"

    Company.objects.filter(pk=data["company"].pk).update(trr=original_iban)
    client.post("/mesecni-obracun/", {"action": "refresh_review"})
    data["batch"].refresh_from_db()

    assert MonthlyCycle.objects.get().trenutni_korak == MonthlyCycle.Korak.PAKETNI_PREGLED
    assert data["batch"].preflight_status == MonthlyBatch.PreflightStatus.PASSED
    assert data["batch"].calculation_status == MonthlyBatch.CalculationStatus.CALCULATED


def test_step4_late_join_preserves_existing_active_snapshot(ready_company, client):
    data = ready_company
    second_company, second_batch = _add_ready_company(data)
    second_iban = second_company.trr
    Company.objects.filter(pk=second_company.pk).update(trr="SI00NEVELJAVEN")
    preveri_predhodno_kontrolo(data["cycle"])
    client.post("/mesecni-obracun/", {"action": "continue_step3"})
    client.post("/mesecni-obracun/", {"action": "continue_step4"})
    data["batch"].refresh_from_db()
    original_snapshot = data["batch"].calculation_snapshot
    original_calculated_at = data["batch"].calculated_at

    LeaseItem.objects.filter(pk=data["item"].pk).update(
        znesek_neto_mesecno=Decimal("125.00")
    )
    potrdi_vzorec_podjetja(data["company"], data["lease"])
    Company.objects.filter(pk=second_company.pk).update(trr=second_iban)

    client.post("/mesecni-obracun/", {"action": "refresh_review"})
    data["batch"].refresh_from_db()
    second_batch.refresh_from_db()

    assert data["batch"].preflight_status == MonthlyBatch.PreflightStatus.PASSED
    assert data["batch"].calculation_status == MonthlyBatch.CalculationStatus.CALCULATED
    assert data["batch"].calculation_snapshot == original_snapshot
    assert data["batch"].calculated_at == original_calculated_at
    assert second_batch.preflight_status == MonthlyBatch.PreflightStatus.PASSED
    assert second_batch.calculation_status == MonthlyBatch.CalculationStatus.CALCULATED
    assert MonthlyCycle.objects.get().trenutni_korak == MonthlyCycle.Korak.PAKETNI_PREGLED


def test_step4_service_rejects_full_recalculation(ready_company, client):
    data = ready_company
    preveri_predhodno_kontrolo(data["cycle"])
    client.post("/mesecni-obracun/", {"action": "continue_step3"})
    client.post("/mesecni-obracun/", {"action": "continue_step4"})
    data["batch"].refresh_from_db()
    snapshot = data["batch"].calculation_snapshot
    calculated_at = data["batch"].calculated_at

    with pytest.raises(NeveljavenPrehodMesecnegaCikla):
        izracunaj_mesecne_kandidate(data["cycle"])

    data["batch"].refresh_from_db()
    assert data["batch"].calculation_snapshot == snapshot
    assert data["batch"].calculated_at == calculated_at


def test_step4_http_rejects_generic_full_recalculation(ready_company, client):
    data = ready_company
    preveri_predhodno_kontrolo(data["cycle"])
    client.post("/mesecni-obracun/", {"action": "continue_step3"})
    client.post("/mesecni-obracun/", {"action": "continue_step4"})
    data["batch"].refresh_from_db()
    snapshot = data["batch"].calculation_snapshot
    calculated_at = data["batch"].calculated_at

    response = client.post(
        "/mesecni-obracun/", {"action": "rerun_calculation"}
    )

    data["batch"].refresh_from_db()
    assert response.status_code == 302
    assert data["batch"].calculation_snapshot == snapshot
    assert data["batch"].calculated_at == calculated_at


def test_stepper_marks_step3_and_step4_states(ready_company, client):
    data = ready_company
    preveri_predhodno_kontrolo(data["cycle"])

    step3 = client.post(
        "/mesecni-obracun/", {"action": "continue_step3"}, follow=True
    ).content.decode()
    assert (
        'class="wizard-step is-active" aria-current="step"><span>3</span>'
        in step3
    )

    step4 = client.post(
        "/mesecni-obracun/", {"action": "continue_step4"}, follow=True
    ).content.decode()
    assert 'class="wizard-step is-completed"><span>✓</span><strong>Izračun osnutkov' in step4
    assert (
        'class="wizard-step is-active" aria-current="step"><span>4</span>'
        in step4
    )

    Company.objects.filter(pk=data["company"].pk).update(trr="SI00NEVELJAVEN")
    refreshed = client.post(
        "/mesecni-obracun/", {"action": "refresh_review"}, follow=True
    ).content.decode()
    assert 'class="wizard-step is-attention"><span>!</span><strong>Izračun osnutkov' in refreshed
    assert (
        'class="wizard-step is-active" aria-current="step"><span>4</span>'
        in refreshed
    )


def test_not_calculated_does_not_show_stale_calculated_timestamp(ready_company, client):
    data = ready_company
    preveri_predhodno_kontrolo(data["cycle"])
    client.post("/mesecni-obracun/", {"action": "continue_step3"})
    Company.objects.filter(pk=data["company"].pk).update(trr="SI00NEVELJAVEN")

    content = client.post(
        "/mesecni-obracun/", {"action": "rerun_preflight"}, follow=True
    ).content.decode()
    data["batch"].refresh_from_db()

    assert data["batch"].calculated_at is not None
    assert data["batch"].calculation_status == MonthlyBatch.CalculationStatus.NOT_CALCULATED
    assert "Izračunano " not in content


def test_preflight_block_invalidates_stale_calculation_until_explicit_rerun(
    ready_company, client
):
    data = ready_company
    original_iban = data["company"].trr
    preveri_predhodno_kontrolo(data["cycle"])
    client.post("/mesecni-obracun/", {"action": "continue_step3"})
    data["batch"].refresh_from_db()
    old_snapshot = data["batch"].calculation_snapshot
    Company.objects.filter(pk=data["company"].pk).update(trr="SI00NEVELJAVEN")

    client.post("/mesecni-obracun/", {"action": "rerun_preflight"})
    data["batch"].refresh_from_db()
    client.post("/mesecni-obracun/", {"action": "continue_step4"})

    assert data["batch"].preflight_status == MonthlyBatch.PreflightStatus.BLOCKED
    assert data["batch"].calculation_status == MonthlyBatch.CalculationStatus.NOT_CALCULATED
    assert data["batch"].calculation_snapshot == old_snapshot
    assert MonthlyCycle.objects.get().trenutni_korak == MonthlyCycle.Korak.IZRACUN_OSNUTKOV

    Company.objects.filter(pk=data["company"].pk).update(trr=original_iban)
    client.post("/mesecni-obracun/", {"action": "rerun_preflight"})
    data["batch"].refresh_from_db()
    assert data["batch"].preflight_status == MonthlyBatch.PreflightStatus.PASSED
    assert data["batch"].calculation_status == MonthlyBatch.CalculationStatus.NOT_CALCULATED

    client.post("/mesecni-obracun/", {"action": "rerun_calculation"})
    data["batch"].refresh_from_db()
    assert data["batch"].calculation_status == MonthlyBatch.CalculationStatus.CALCULATED


def test_step4_refresh_excludes_newly_blocked_stale_member(ready_company, client):
    data = ready_company
    _add_ready_company(data)
    preveri_predhodno_kontrolo(data["cycle"])
    client.post("/mesecni-obracun/", {"action": "continue_step3"})
    client.post("/mesecni-obracun/", {"action": "continue_step4"})
    Company.objects.filter(pk=data["company"].pk).update(trr="SI00NEVELJAVEN")

    response = client.post(
        "/mesecni-obracun/", {"action": "refresh_review"}, follow=True
    )
    data["batch"].refresh_from_db()
    rendered_batch = next(
        batch for batch in response.context["batches"] if batch.pk == data["batch"].pk
    )

    assert data["batch"].preflight_status == MonthlyBatch.PreflightStatus.BLOCKED
    assert data["batch"].calculation_status == MonthlyBatch.CalculationStatus.NOT_CALCULATED
    assert rendered_batch.is_active_calculation is False
    assert rendered_batch.calculation_summary["candidates"] == 0


def test_candidate_review_unique_constraint(ready_company):
    data = ready_company
    _candidate, review = _enter_step4_with_reviews(data)
    with pytest.raises(IntegrityError):
        MonthlyCandidateReview.objects.create(
            batch=review.batch,
            lease=review.lease,
            candidate_fingerprint=review.candidate_fingerprint,
            selected=True,
            requires_confirmation=False,
        )


@pytest.mark.parametrize(
    ("will_issue", "exceptions", "selected", "requires_confirmation"),
    [
        (True, [], True, False),
        (True, [{"code": "TEST"}], True, True),
        (False, [{"code": "TEST"}], False, False),
    ],
)
def test_candidate_review_defaults(
    ready_company, will_issue, exceptions, selected, requires_confirmation
):
    _candidate, review = _enter_step4_with_reviews(
        ready_company, will_issue=will_issue, exceptions=exceptions
    )
    assert review.selected is selected
    assert review.requires_confirmation is requires_confirmation
    assert review.confirmed_at is None
    assert review.non_issue_reason == ""
    assert review.reviewed_at is None


def test_review_sync_is_idempotent_and_preserves_decisions(ready_company):
    data = ready_company
    _candidate, review = _enter_step4_with_reviews(data)
    stamp = timezone.now()
    MonthlyCandidateReview.objects.filter(pk=review.pk).update(
        selected=False,
        confirmed_at=stamp,
        non_issue_reason="Ročna odločitev",
        reviewed_at=stamp,
    )

    uskladi_preglede_mesecnega_cikla(data["cycle"])
    reopened = MonthlyCandidateReview.objects.get()

    assert MonthlyCandidateReview.objects.count() == 1
    assert (reopened.selected, reopened.confirmed_at, reopened.non_issue_reason, reopened.reviewed_at) == (
        False, stamp, "Ročna odločitev", stamp
    )


def test_step4_http_refresh_preserves_user_decision(ready_company, client):
    data = ready_company
    _candidate, review = _enter_step4_with_reviews(data)
    review = nastavi_izbiro_kandidata(
        data["cycle"],
        lease_id=review.lease_id,
        candidate_fingerprint=review.candidate_fingerprint,
        selected=False,
        non_issue_reason="Ne izdaj",
    )
    expected = (
        review.pk,
        review.candidate_fingerprint,
        review.selected,
        review.confirmed_at,
        review.non_issue_reason,
        review.reviewed_at,
    )

    response = client.post("/mesecni-obracun/", {"action": "refresh_review"})
    current = trenutni_pregledi_mesecnega_cikla(data["cycle"])

    assert response.status_code == 302
    assert len(current) == 1
    review = current[0]
    assert (
        review.pk,
        review.candidate_fingerprint,
        review.selected,
        review.confirmed_at,
        review.non_issue_reason,
        review.reviewed_at,
    ) == expected


def test_step4_http_refresh_adds_late_join_review_without_resetting_existing(
    ready_company, client
):
    data = ready_company
    second_company, second_batch = _add_ready_company(data)
    second_lease = Lease.objects.get(unit__property__company=second_company)
    second_iban = second_company.trr
    Company.objects.filter(pk=second_company.pk).update(trr="SI00NEVELJAVEN")
    preveri_predhodno_kontrolo(data["cycle"])
    client.post("/mesecni-obracun/", {"action": "continue_step3"})
    client.post("/mesecni-obracun/", {"action": "continue_step4"})
    data["batch"].refresh_from_db()
    original_snapshot = data["batch"].calculation_snapshot
    first_review = MonthlyCandidateReview.objects.get(batch=data["batch"])
    first_review = nastavi_izbiro_kandidata(
        data["cycle"],
        lease_id=first_review.lease_id,
        candidate_fingerprint=first_review.candidate_fingerprint,
        selected=False,
        non_issue_reason="Ne izdaj A",
    )

    Company.objects.filter(pk=second_company.pk).update(trr=second_iban)
    response = client.post("/mesecni-obracun/", {"action": "refresh_review"})
    data["batch"].refresh_from_db()
    second_batch.refresh_from_db()
    current = trenutni_pregledi_mesecnega_cikla(data["cycle"])

    assert response.status_code == 302
    assert data["batch"].calculation_snapshot == original_snapshot
    preserved = next(review for review in current if review.batch_id == data["batch"].pk)
    assert preserved.pk == first_review.pk
    assert preserved.candidate_fingerprint == first_review.candidate_fingerprint
    assert preserved.selected is False
    assert preserved.non_issue_reason == "Ne izdaj A"
    assert preserved.reviewed_at == first_review.reviewed_at
    assert second_batch.calculation_status == MonthlyBatch.CalculationStatus.CALCULATED
    joined = next(review for review in current if review.batch_id == second_batch.pk)
    assert joined.lease_id == second_lease.pk
    assert len(current) == 2
    assert {review.pk for review in current} == set(
        MonthlyCandidateReview.objects.values_list("pk", flat=True)
    )


def test_changed_fingerprint_creates_fresh_review_and_keeps_stale(ready_company):
    data = ready_company
    candidate, old = _enter_step4_with_reviews(data)
    MonthlyCandidateReview.objects.filter(pk=old.pk).update(
        selected=False, non_issue_reason="Ne izdaj", reviewed_at=timezone.now()
    )
    snapshot = data["batch"].calculation_snapshot
    snapshot["candidates"][0]["candidate_fingerprint"] = "b" * 64
    data["batch"].calculation_snapshot = snapshot
    data["batch"].save(update_fields=["calculation_snapshot"])

    current = uskladi_preglede_mesecnega_cikla(data["cycle"])

    assert MonthlyCandidateReview.objects.count() == 2
    assert len(current) == 1
    assert current[0].candidate_fingerprint == "b" * 64
    assert current[0].selected is True
    assert current[0].non_issue_reason == ""
    assert MonthlyCandidateReview.objects.filter(
        candidate_fingerprint=candidate["candidate_fingerprint"]
    ).exists()


def test_blocked_review_stays_stored_but_is_not_current_and_can_return(ready_company):
    data = ready_company
    _candidate, review = _enter_step4_with_reviews(data)
    MonthlyBatch.objects.filter(pk=data["batch"].pk).update(
        preflight_status=MonthlyBatch.PreflightStatus.BLOCKED
    )
    assert trenutni_pregledi_mesecnega_cikla(data["cycle"]) == []
    assert MonthlyCandidateReview.objects.filter(pk=review.pk).exists()

    MonthlyBatch.objects.filter(pk=data["batch"].pk).update(
        preflight_status=MonthlyBatch.PreflightStatus.PASSED
    )
    uskladi_preglede_mesecnega_cikla(data["cycle"])
    assert [item.pk for item in trenutni_pregledi_mesecnega_cikla(data["cycle"])] == [review.pk]


def test_selection_and_confirmation_guards(ready_company):
    data = ready_company
    _candidate, review = _enter_step4_with_reviews(
        data, exceptions=[{"code": "TEST"}]
    )
    args = {
        "lease_id": review.lease_id,
        "candidate_fingerprint": review.candidate_fingerprint,
    }
    with pytest.raises(ValidationError):
        nastavi_izbiro_kandidata(data["cycle"], selected=False, **args)

    review = nastavi_izbiro_kandidata(
        data["cycle"], selected=False, non_issue_reason="  Ne izdaj  ", **args
    )
    assert review.non_issue_reason == "Ne izdaj"
    assert review.reviewed_at is not None
    assert review.confirmed_at is None
    with pytest.raises(ValidationError):
        potrdi_izjemo_kandidata(data["cycle"], **args)

    review = nastavi_izbiro_kandidata(data["cycle"], selected=True, **args)
    assert review.non_issue_reason == ""
    review = potrdi_izjemo_kandidata(data["cycle"], **args)
    assert review.confirmed_at is not None
    assert review.reviewed_at == review.confirmed_at

    args["candidate_fingerprint"] = "c" * 64
    with pytest.raises(ValidationError):
        nastavi_izbiro_kandidata(data["cycle"], selected=True, **args)


def test_nonissuable_cannot_be_selected_and_plain_candidate_cannot_be_confirmed(ready_company):
    data = ready_company
    _candidate, review = _enter_step4_with_reviews(data, will_issue=False)
    args = {
        "lease_id": review.lease_id,
        "candidate_fingerprint": review.candidate_fingerprint,
    }
    with pytest.raises(ValidationError):
        nastavi_izbiro_kandidata(data["cycle"], selected=True, **args)
    with pytest.raises(ValidationError):
        potrdi_izjemo_kandidata(data["cycle"], **args)


def test_selected_plain_candidate_cannot_be_confirmed(ready_company):
    data = ready_company
    _candidate, review = _enter_step4_with_reviews(data)
    with pytest.raises(ValidationError):
        potrdi_izjemo_kandidata(
            data["cycle"],
            lease_id=review.lease_id,
            candidate_fingerprint=review.candidate_fingerprint,
        )


@pytest.mark.parametrize(
    "step",
    [
        MonthlyCycle.Korak.IZBIRA_MESECA,
        MonthlyCycle.Korak.PREDHODNA_KONTROLA,
        MonthlyCycle.Korak.IZRACUN_OSNUTKOV,
        MonthlyCycle.Korak.POTRDITEV_PAKETA,
        MonthlyCycle.Korak.ZAKLJUCEK,
    ],
)
def test_review_services_reject_outside_step4(ready_company, step):
    data = ready_company
    data["cycle"].trenutni_korak = step
    data["cycle"].save(update_fields=["trenutni_korak"])
    with pytest.raises(NeveljavenPrehodMesecnegaCikla):
        uskladi_preglede_mesecnega_cikla(data["cycle"])


@pytest.mark.parametrize(
    "step",
    [MonthlyCycle.Korak.IZRACUN_OSNUTKOV, MonthlyCycle.Korak.POTRDITEV_PAKETA],
)
@pytest.mark.parametrize("operation", ["select", "confirm"])
def test_review_mutations_reject_outside_step4(ready_company, step, operation):
    data = ready_company
    _candidate, review = _enter_step4_with_reviews(
        data, exceptions=[{"code": "TEST"}]
    )
    data["cycle"].trenutni_korak = step
    data["cycle"].save(update_fields=["trenutni_korak"])
    args = {
        "lease_id": review.lease_id,
        "candidate_fingerprint": review.candidate_fingerprint,
    }

    with pytest.raises(NeveljavenPrehodMesecnegaCikla):
        if operation == "select":
            nastavi_izbiro_kandidata(data["cycle"], selected=True, **args)
        else:
            potrdi_izjemo_kandidata(data["cycle"], **args)


def test_sync_has_no_invoice_or_numbering_side_effects(ready_company):
    before = list(NumberSequence.objects.values_list("pk", "zadnja_stevilka"))
    _enter_step4_with_reviews(ready_company)
    assert not Invoice.objects.exists()
    assert not InvoiceLine.objects.exists()
    assert list(NumberSequence.objects.values_list("pk", "zadnja_stevilka")) == before


@pytest.mark.parametrize("step", [MonthlyCycle.Korak.POTRDITEV_PAKETA, MonthlyCycle.Korak.ZAKLJUCEK])
def test_step5_and_step6_freeze_preflight_and_calculation(ready_company, client, step):
    data = ready_company
    preveri_predhodno_kontrolo(data["cycle"])
    data["cycle"].trenutni_korak = MonthlyCycle.Korak.IZRACUN_OSNUTKOV
    data["cycle"].save(update_fields=["trenutni_korak"])
    izracunaj_mesecne_kandidate(data["cycle"])
    data["batch"].refresh_from_db()
    snapshot = data["batch"].calculation_snapshot
    checked_at = data["batch"].preflight_checked_at
    calculated_at = data["batch"].calculated_at
    data["cycle"].trenutni_korak = step
    data["cycle"].save(update_fields=["trenutni_korak"])
    Company.objects.filter(pk=data["company"].pk).update(trr="SI00NEVELJAVEN")

    client.post("/mesecni-obracun/", {"action": "rerun_preflight"})
    client.post("/mesecni-obracun/", {"action": "rerun_calculation"})
    data["batch"].refresh_from_db()

    assert data["batch"].preflight_checked_at == checked_at
    assert data["batch"].calculated_at == calculated_at
    assert data["batch"].calculation_snapshot == snapshot


def test_valid_company_passes_and_rerun_replaces_snapshot(ready_company):
    data = ready_company
    before_sequences = list(NumberSequence.objects.values_list("pk", "zadnja_stevilka"))
    assert not (Invoice.objects.exists() or InvoiceLine.objects.exists())

    preveri_predhodno_kontrolo(data["cycle"])
    data["batch"].refresh_from_db()
    first_checked = data["batch"].preflight_checked_at
    assert data["batch"].preflight_status == MonthlyBatch.PreflightStatus.PASSED

    Company.objects.filter(pk=data["company"].pk).update(ddv_zavezanec=False)
    preveri_predhodno_kontrolo(data["cycle"])
    assert "DDV_POTRDITEV_ZASTARELA" in _codes(data["batch"])
    data["batch"].refresh_from_db()
    assert data["batch"].preflight_checked_at > first_checked
    assert not (Invoice.objects.exists() or InvoiceLine.objects.exists())
    assert list(NumberSequence.objects.values_list("pk", "zadnja_stevilka")) == before_sequences


def test_stale_sample_blocks(ready_company):
    data = ready_company
    LeaseItem.objects.filter(pk=data["item"].pk).update(
        znesek_neto_mesecno=Decimal("110.00")
    )

    preveri_predhodno_kontrolo(data["cycle"])

    assert "VZOREC_POTRDITEV_ZASTARELA" in _codes(data["batch"])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("trr", "SI00NEVELJAVEN"),
        ("davcna_stevilka", "00000000"),
        ("maticna_stevilka", "123"),
        ("drzava_koda", "XX"),
    ],
)
def test_invalid_company_master_data_blocks(ready_company, field, value):
    data = ready_company
    Company.objects.filter(pk=data["company"].pk).update(**{field: value})

    preveri_predhodno_kontrolo(data["cycle"])
    data["batch"].refresh_from_db()

    assert any(
        issue["code"] == "PODJETJE_NEVELJAVNO" and issue["field"] == field
        for issue in data["batch"].preflight_issues
    )


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"identificiran_za_ddv": False, "davcna_stevilka": ""}, "davcna_stevilka"),
        (
            {"drzava_koda": "AT", "identificiran_za_ddv": True,
             "ddv_identifikacijska_stevilka": ""},
            "ddv_identifikacijska_stevilka",
        ),
        ({"davcna_stevilka": "00000000"}, "davcna_stevilka"),
    ],
)
def test_invalid_tenant_business_data_blocks(ready_company, changes, field):
    data = ready_company
    Tenant.objects.filter(pk=data["tenant"].pk).update(**changes)

    preveri_predhodno_kontrolo(data["cycle"])
    data["batch"].refresh_from_db()

    assert any(
        issue["code"] == "NAJEMNIK_NEVELJAVEN" and issue["field"] == field
        for issue in data["batch"].preflight_issues
    )


def test_malformed_company_is_isolated_from_valid_company(ready_company):
    data = ready_company
    _, valid_batch = _add_ready_company(data)
    Company.objects.filter(pk=data["company"].pk).update(trr="SI00NEVELJAVEN")

    preveri_predhodno_kontrolo(data["cycle"])
    data["batch"].refresh_from_db()
    valid_batch.refresh_from_db()

    assert data["batch"].preflight_status == MonthlyBatch.PreflightStatus.BLOCKED
    assert valid_batch.preflight_status == MonthlyBatch.PreflightStatus.PASSED, valid_batch.preflight_issues


@pytest.mark.parametrize("stale", [False, True])
def test_vat_problem_blocks_only_affected_company(ready_company, stale):
    data = ready_company
    _, valid_batch = _add_ready_company(data)
    if stale:
        Company.objects.filter(pk=data["company"].pk).update(ddv_zavezanec=False)
        expected = "DDV_POTRDITEV_ZASTARELA"
    else:
        data["company"].vat_settings_confirmations.all().delete()
        expected = "DDV_POTRDITEV_MANJKA"

    preveri_predhodno_kontrolo(data["cycle"])
    valid_batch.refresh_from_db()

    assert expected in _codes(data["batch"])
    assert valid_batch.preflight_status == MonthlyBatch.PreflightStatus.PASSED, valid_batch.preflight_issues


@pytest.mark.parametrize(
    ("model", "change", "expected"),
    [
        ("vat", None, "DDV_POTRDITEV_MANJKA"),
        ("sample", None, "VZOREC_POTRDITEV_MANJKA"),
        ("signatory", None, "PODPISNIK_MANJKA"),
    ],
)
def test_missing_company_requirements_block(ready_company, model, change, expected):
    data = ready_company
    if model == "vat":
        data["company"].vat_settings_confirmations.all().delete()
    elif model == "sample":
        data["company"].sample_confirmations.all().delete()
    else:
        data["signatory"].delete()

    preveri_predhodno_kontrolo(data["cycle"])

    assert expected in _codes(data["batch"])
    data["batch"].refresh_from_db()
    assert data["batch"].preflight_status == MonthlyBatch.PreflightStatus.BLOCKED


def test_overlapping_signatories_block(ready_company):
    data = ready_company
    data["signatory"].velja_do = date(2026, 12, 31)
    data["signatory"].save(update_fields=["velja_do"])
    Signatory.objects.create(
        company=data["company"],
        ime_priimek="Drugi Podpisnik",
        podpis_slika="signature.png",
        velja_od=date(2026, 9, 1),
        velja_do=date(2026, 10, 1),
    )

    preveri_predhodno_kontrolo(data["cycle"])

    assert "PODPISNIK_PREKRIVANJE" in _codes(data["batch"])


def test_required_reference_fields_block(ready_company):
    data = ready_company
    VatRegime.objects.filter(pk=data["regime"].pk).update(zahteva_referenco=True)

    preveri_predhodno_kontrolo(data["cycle"])

    assert {"REFERENCA_LISTINE_MANJKA", "DATUM_REFERENCE_MANJKA"} <= _codes(
        data["batch"]
    )


def test_valid_required_reference_passes(ready_company):
    data = ready_company
    VatRegime.objects.filter(pk=data["regime"].pk).update(zahteva_referenco=True)
    LeaseItem.objects.filter(pk=data["item"].pk).update(
        referenca_listine="POG-1", datum_reference=date(2026, 1, 1)
    )
    potrdi_ddv_nastavitve(data["company"])
    potrdi_vzorec_podjetja(data["company"], data["lease"])

    preveri_predhodno_kontrolo(data["cycle"])

    assert "REFERENCA_LISTINE_MANJKA" not in _codes(data["batch"])
    assert "DATUM_REFERENCE_MANJKA" not in _codes(data["batch"])


@pytest.mark.parametrize(
    ("start", "end", "included"),
    [
        (date(2026, 9, 1), None, True),
        (date(2026, 1, 1), date(2026, 9, 30), True),
        (date(2026, 1, 1), date(2026, 8, 31), False),
        (date(2026, 10, 1), None, False),
    ],
)
def test_lease_month_scope(ready_company, start, end, included):
    data = ready_company
    Lease.objects.filter(pk=data["lease"].pk).update(
        datum_zacetka=start,
        tip_trajanja=(
            Lease.TipTrajanja.DOLOCEN_CAS if end else Lease.TipTrajanja.NEDOLOCEN_CAS
        ),
        datum_konca=end,
    )
    LeaseItem.objects.filter(pk=data["item"].pk).update(znesek_neto_mesecno=0)

    preveri_predhodno_kontrolo(data["cycle"])

    assert ("POSTAVKA_NEVELJAVNA" in _codes(data["batch"])) is included


def test_proportional_and_expiry_are_warnings_only(ready_company):
    data = ready_company
    Lease.objects.filter(pk=data["lease"].pk).update(
        datum_zacetka=date(2026, 9, 10),
        tip_trajanja=Lease.TipTrajanja.DOLOCEN_CAS,
        datum_konca=date(2026, 9, 30),
    )
    potrdi_vzorec_podjetja(data["company"], data["lease"])

    preveri_predhodno_kontrolo(data["cycle"])
    data["batch"].refresh_from_db()

    assert data["batch"].preflight_status == MonthlyBatch.PreflightStatus.PASSED
    assert {"SORAZMERNO_OBDOBJE_POGODBE", "POGODBA_POTECE_V_MESECU"} <= _codes(
        data["batch"]
    )
    assert all(issue["severity"] == "WARNING" for issue in data["batch"].preflight_issues)


def test_inactive_chain_is_excluded(ready_company):
    data = ready_company
    Unit.objects.filter(pk=data["unit"].pk).update(aktivna=False)
    LeaseItem.objects.filter(pk=data["item"].pk).update(znesek_neto_mesecno=0)

    preveri_predhodno_kontrolo(data["cycle"])

    assert "POSTAVKA_NEVELJAVNA" not in _codes(data["batch"])


@pytest.mark.parametrize(("model", "field"), [(Property, "aktivna"), (Unit, "aktivna"), (Tenant, "aktiven")])
def test_each_inactive_chain_member_is_excluded(ready_company, model, field):
    data = ready_company
    key = {Property: "property", Unit: "unit", Tenant: "tenant"}[model]
    model.objects.filter(pk=data[key].pk).update(**{field: False})
    LeaseItem.objects.filter(pk=data["item"].pk).update(znesek_neto_mesecno=0)

    preveri_predhodno_kontrolo(data["cycle"])

    assert "POSTAVKA_NEVELJAVNA" not in _codes(data["batch"])


@pytest.mark.parametrize(
    ("start", "end", "included"),
    [
        (date(2026, 9, 1), None, True),
        (None, date(2026, 9, 30), True),
        (None, date(2026, 8, 31), False),
        (date(2026, 10, 1), None, False),
    ],
)
def test_lease_item_month_boundaries(ready_company, start, end, included):
    data = ready_company
    LeaseItem.objects.filter(pk=data["item"].pk).update(
        datum_zacetka_postavke=start,
        datum_prenehanja_postavke=end,
        znesek_neto_mesecno=0,
    )

    preveri_predhodno_kontrolo(data["cycle"])

    assert ("POSTAVKA_NEVELJAVNA" in _codes(data["batch"])) is included


def test_company_issue_is_deduplicated_across_multiple_leases(ready_company):
    data = ready_company
    second = Lease.objects.create(
        unit=data["unit"], tenant=data["tenant"], oznaka_pogodbe="P-2",
        tip_trajanja=Lease.TipTrajanja.NEDOLOCEN_CAS,
        datum_zacetka=date(2026, 1, 1), rok_placila_dni=15,
    )
    LeaseItem.objects.create(
        lease=second, znesek_neto_mesecno=Decimal("50.00"), vat_regime=data["regime"]
    )
    Company.objects.filter(pk=data["company"].pk).update(trr="SI00NEVELJAVEN")

    preveri_predhodno_kontrolo(data["cycle"])
    data["batch"].refresh_from_db()

    matches = [
        issue for issue in data["batch"].preflight_issues
        if issue["code"] == "PODJETJE_NEVELJAVNO" and issue["field"] == "trr"
    ]
    assert len(matches) == 1


def test_rerun_replaces_old_issues(ready_company):
    data = ready_company
    MonthlyBatch.objects.filter(pk=data["batch"].pk).update(
        preflight_issues=[{
            "code": "OLD", "severity": "BLOCKER", "message": "Old",
            "model": "Company", "object_id": data["company"].pk, "field": None,
        }]
    )

    preveri_predhodno_kontrolo(data["cycle"])

    assert "OLD" not in _codes(data["batch"])


def test_ui_shows_precise_contract_location_and_persists_for_fresh_client(
    ready_company, client
):
    data = ready_company
    LeaseItem.objects.filter(pk=data["item"].pk).update(znesek_neto_mesecno=0)
    preveri_predhodno_kontrolo(data["cycle"])

    from django.test import Client

    content = Client().get("/mesecni-obracun/").content.decode()
    data["batch"].refresh_from_db()
    amount_issue = next(
        issue
        for issue in data["batch"].preflight_issues
        if issue["field"] == "znesek_neto_mesecno"
    )

    assert "Pogodbe → P-2026" in content
    assert amount_issue["message"] in content


def test_frozen_selected_candidate_is_snapshot_only_and_idempotent(
    ready_company, settings
):
    data = ready_company
    RecipientContact.objects.create(
        lease=data["lease"], email="kontakt@example.com", vloga="TO"
    )
    candidate, _review = _freeze_ready_package(data)
    frozen = data["batch"].calculation_snapshot
    Company.objects.filter(pk=data["company"].pk).update(
        naziv="Changed", naslov="Changed 9", trr="SI56191000000999999"
    )
    Tenant.objects.filter(pk=data["tenant"].pk).update(
        naziv="Changed tenant", naslov="Changed 3"
    )
    Lease.objects.filter(pk=data["lease"].pk).update(rok_placila_dni=90)
    LeaseItem.objects.filter(pk=data["item"].pk).update(
        naziv_postavke="Changed", znesek_neto_mesecno=Decimal("999.00")
    )
    VatRegime.objects.filter(pk=data["regime"].pk).update(
        stopnja=Decimal("9.50"), nacin_obracuna=VatRegime.NacinObracuna.OPROSCENO
    )
    settings.PDF_TEMPLATE_VERSION = "changed-after-freeze"
    Signatory.objects.filter(pk=data["signatory"].pk).update(
        ime_priimek="Changed signer"
    )
    RecipientContact.objects.filter(lease=data["lease"]).update(
        email="changed@example.com"
    )

    first = izdaj_zamrznjeni_mesecni_paket(data["cycle"])[0]
    second = izdaj_zamrznjeni_mesecni_paket(data["cycle"])[0]
    invoice = Invoice.objects.get()
    line = InvoiceLine.objects.get()

    assert first["ok"] and not first["reused"]
    assert second["ok"] and second["reused"]
    assert (Invoice.objects.count(), InvoiceLine.objects.count()) == (1, 1)
    assert invoice.stevilka == "NP-00001"
    assert invoice.status == Invoice.Status.IZBRAN_ZA_POSILJANJE
    assert invoice.prejemnik_naziv == candidate["tenant"]["name"]
    assert invoice.izdajatelj_naziv == frozen["company"]["name"]
    assert invoice.rok_placila.isoformat() == candidate["due_date"]
    assert invoice.pdf_template_version == candidate["pdf_template_version"]
    assert invoice.izdajatelj_podpisnik_ime == frozen["company"]["signatory"]["name"]
    assert invoice.izdajatelj_logotip_slika_pot == (
        f"nespremenljivi-asseti/{frozen['company']['logo_checksum']}"
    )
    assert invoice.izdajatelj_podpisnik_slika_pot == (
        "nespremenljivi-asseti/"
        f"{frozen['company']['signatory']['signature_checksum']}"
    )
    assert invoice.dostavni_kontakti_snapshot == candidate["delivery_contacts"]
    assert line.opis == candidate["lines"][0]["description"]
    assert line.osnova == Decimal(candidate["lines"][0]["net"])
    assert line.ddv_eslog_koda == ""
    assert NumberSequence.objects.get().zadnja_stevilka == 1
    assert invoice.rendered_pdf_path == invoice.checksum == ""
    assert invoice.rendered_eslog_path == invoice.rendered_eslog_checksum == ""
    assert MonthlyCycle.objects.get().trenutni_korak == 5


def test_frozen_unselected_candidate_archives_without_number(ready_company):
    data = ready_company
    candidate = _calculate(data)[0]
    data["cycle"].trenutni_korak = MonthlyCycle.Korak.PAKETNI_PREGLED
    data["cycle"].save(update_fields=["trenutni_korak"])
    uskladi_preglede_mesecnega_cikla(data["cycle"])
    review = MonthlyCandidateReview.objects.get()
    nastavi_izbiro_kandidata(
        data["cycle"], lease_id=review.lease_id,
        candidate_fingerprint=review.candidate_fingerprint,
        selected=False, non_issue_reason="User decision",
    )
    zamrzni_paket_za_potrditev(data["cycle"])

    assert izdaj_zamrznjeni_mesecni_paket(data["cycle"])[0]["ok"]
    invoice = Invoice.objects.get()
    assert invoice.status == Invoice.Status.ARHIVIRAN_NI_IZDAN
    assert invoice.stevilka is None
    assert invoice.razlog_arhiviranja == "User decision"
    assert not NumberSequence.objects.exists()
    assert InvoiceLine.objects.count() == len(candidate["lines"])


def test_post_allocation_failure_rolls_back_number_and_invoice(
    ready_company, monkeypatch
):
    data = ready_company
    candidate, review = _freeze_ready_package(data)

    def fail_create(**_kwargs):
        raise IntegrityError("forced failure")

    monkeypatch.setattr(Invoice.objects, "create", fail_create)
    with pytest.raises(IntegrityError, match="forced failure"):
        materializiraj_zamrznjenega_mesecnega_kandidata(
            data["batch"].pk, candidate["lease_id"], review.candidate_fingerprint
        )
    assert not NumberSequence.objects.exists()
    assert not Invoice.objects.exists()
    assert not InvoiceLine.objects.exists()


def test_retry_reuses_invoice_after_pdf_metadata(ready_company):
    data = ready_company
    candidate, review = _freeze_ready_package(data)
    invoice, reused = materializiraj_zamrznjenega_mesecnega_kandidata(
        data["batch"].pk, candidate["lease_id"], review.candidate_fingerprint
    )
    assert not reused
    invoice.rendered_pdf_path = "arhiv/NP-00001.pdf"
    invoice.checksum = "a" * 64
    invoice.save(update_fields=["rendered_pdf_path", "checksum"])

    same, reused = materializiraj_zamrznjenega_mesecnega_kandidata(
        data["batch"].pk, candidate["lease_id"], review.candidate_fingerprint
    )

    assert reused and same.pk == invoice.pk
    assert NumberSequence.objects.get().zadnja_stevilka == 1
    assert (Invoice.objects.count(), InvoiceLine.objects.count()) == (1, 1)


def test_six_digit_np_number_is_materialized_and_reused(ready_company):
    data = ready_company
    candidate, review = _freeze_ready_package(data)
    NumberSequence.objects.create(
        company=data["company"],
        tip=NumberSequence.Tip.NP,
        leto=0,
        zadnja_stevilka=99999,
    )

    invoice, reused = materializiraj_zamrznjenega_mesecnega_kandidata(
        data["batch"].pk, candidate["lease_id"], review.candidate_fingerprint
    )
    same, reused_again = materializiraj_zamrznjenega_mesecnega_kandidata(
        data["batch"].pk, candidate["lease_id"], review.candidate_fingerprint
    )
    state = stanje_zamrznjenega_mesecnega_kandidata(
        data["batch"].pk, candidate["lease_id"], review.candidate_fingerprint
    )

    assert invoice.stevilka == "NP-100000"
    assert not reused and reused_again and same.pk == invoice.pk
    assert (Invoice.objects.count(), InvoiceLine.objects.count()) == (1, 1)
    assert NumberSequence.objects.get().zadnja_stevilka == 100000
    assert state["state"] == "MATERIALIZED" and state["invoice"].pk == invoice.pk

    invoice.stevilka = "NP-1234"
    invoice.save(update_fields=["stevilka"])
    assert stanje_zamrznjenega_mesecnega_kandidata(
        data["batch"].pk, candidate["lease_id"], review.candidate_fingerprint
    )["state"] == "CONFLICT"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("lease_id", None),
        ("razlog_rocnega_popravka", "Unrelated adjustment"),
        ("izvirno_izracunana_vrednost", Decimal("100.00")),
    ],
)
def test_conflicting_line_trace_or_audit_state_fails_closed(
    ready_company, field, value
):
    data = ready_company
    candidate, review = _freeze_ready_package(data)
    invoice, _reused = materializiraj_zamrznjenega_mesecnega_kandidata(
        data["batch"].pk, candidate["lease_id"], review.candidate_fingerprint
    )
    InvoiceLine.objects.filter(invoice=invoice).update(**{field: value})

    state = stanje_zamrznjenega_mesecnega_kandidata(
        data["batch"].pk, candidate["lease_id"], review.candidate_fingerprint
    )

    assert state["state"] == "CONFLICT"
    with pytest.raises(ValidationError, match="konfliktu"):
        materializiraj_zamrznjenega_mesecnega_kandidata(
            data["batch"].pk, candidate["lease_id"], review.candidate_fingerprint
        )
    assert NumberSequence.objects.get().zadnja_stevilka == 1


def test_line_creation_failure_rolls_back_invoice_and_number(
    ready_company, monkeypatch
):
    data = ready_company
    candidate, review = _freeze_ready_package(data)

    def fail_bulk_create(_lines):
        raise IntegrityError("forced line failure")

    monkeypatch.setattr(InvoiceLine.objects, "bulk_create", fail_bulk_create)
    with pytest.raises(IntegrityError, match="forced line failure"):
        materializiraj_zamrznjenega_mesecnega_kandidata(
            data["batch"].pk, candidate["lease_id"], review.candidate_fingerprint
        )

    assert not NumberSequence.objects.exists()
    assert not Invoice.objects.exists()
    assert not InvoiceLine.objects.exists()


def test_direct_materialization_rejects_blocked_stale_batch(ready_company):
    data = ready_company
    _company, stale_batch = _add_ready_company(data)
    candidates = _calculate(data)
    data["cycle"].trenutni_korak = MonthlyCycle.Korak.PAKETNI_PREGLED
    data["cycle"].save(update_fields=["trenutni_korak"])
    uskladi_preglede_mesecnega_cikla(data["cycle"])
    stale_review = MonthlyCandidateReview.objects.get(batch=stale_batch)
    active_review = MonthlyCandidateReview.objects.get(batch=data["batch"])
    for review in (stale_review, active_review):
        if review.requires_confirmation:
            potrdi_izjemo_kandidata(
                data["cycle"], lease_id=review.lease_id,
                candidate_fingerprint=review.candidate_fingerprint,
            )
    stale_batch.preflight_status = MonthlyBatch.PreflightStatus.BLOCKED
    stale_batch.calculation_status = MonthlyBatch.CalculationStatus.BLOCKED
    stale_batch.save(update_fields=["preflight_status", "calculation_status"])
    zamrzni_paket_za_potrditev(data["cycle"])

    with pytest.raises(ValidationError, match="aktivnemu zamrznjenemu"):
        materializiraj_zamrznjenega_mesecnega_kandidata(
            stale_batch.pk,
            stale_review.lease_id,
            stale_review.candidate_fingerprint,
        )
    active_candidate = next(
        item for item in candidates if item["lease_id"] == active_review.lease_id
    )
    invoice, reused = materializiraj_zamrznjenega_mesecnega_kandidata(
        data["batch"].pk,
        active_candidate["lease_id"],
        active_review.candidate_fingerprint,
    )
    assert not reused and invoice.stevilka == "NP-00001"
    assert Invoice.objects.count() == 1
    assert NumberSequence.objects.get().zadnja_stevilka == 1


def test_partial_package_continues_and_retry_reuses_resolved_candidates(
    ready_company,
):
    data = ready_company
    _add_lease(data, "P-B", with_item=True)
    _add_lease(data, "P-C", with_item=True)
    potrdi_ddv_nastavitve(data["company"])
    potrdi_vzorec_podjetja(data["company"], data["lease"])
    candidates, _reviews = _freeze_all_candidates(data)
    snapshot = data["batch"].calculation_snapshot
    snapshot["candidates"][1]["candidate_fingerprint"] = "0" * 64
    data["batch"].calculation_snapshot = snapshot
    data["batch"].save(update_fields=["calculation_snapshot"])

    first = izdaj_zamrznjeni_mesecni_paket(data["cycle"])
    first_numbers = list(
        Invoice.objects.order_by("lease_id").values_list("stevilka", flat=True)
    )
    second = izdaj_zamrznjeni_mesecni_paket(data["cycle"])

    assert [result["ok"] for result in first] == [True, False, True]
    assert [result["ok"] for result in second] == [True, False, True]
    assert first_numbers == ["NP-00001", "NP-00002"]
    assert list(
        Invoice.objects.order_by("lease_id").values_list("stevilka", flat=True)
    ) == first_numbers
    assert NumberSequence.objects.get().zadnja_stevilka == 2
    assert len(candidates) == 3


def test_zero_selected_including_system_zero_lines_consumes_no_number(
    ready_company,
):
    data = ready_company
    _add_lease(data, "P-NO-LINES")
    candidates = _calculate(data)
    data["cycle"].trenutni_korak = MonthlyCycle.Korak.PAKETNI_PREGLED
    data["cycle"].save(update_fields=["trenutni_korak"])
    uskladi_preglede_mesecnega_cikla(data["cycle"])
    selected = MonthlyCandidateReview.objects.get(selected=True)
    nastavi_izbiro_kandidata(
        data["cycle"], lease_id=selected.lease_id,
        candidate_fingerprint=selected.candidate_fingerprint,
        selected=False, non_issue_reason="User decision",
    )
    zamrzni_paket_za_potrditev(data["cycle"])

    results = izdaj_zamrznjeni_mesecni_paket(data["cycle"])

    assert all(result["ok"] for result in results)
    assert Invoice.objects.count() == 2
    assert not NumberSequence.objects.exists()
    assert set(Invoice.objects.values_list("status", flat=True)) == {
        Invoice.Status.ARHIVIRAN_NI_IZDAN
    }
    no_lines = next(item for item in candidates if not item["lines"])
    assert not InvoiceLine.objects.filter(lease_id=no_lines["lease_id"]).exists()


@pytest.mark.parametrize("step", [MonthlyCycle.Korak.PAKETNI_PREGLED, MonthlyCycle.Korak.ZAKLJUCEK])
def test_direct_issuance_rejects_step4_and_step6(ready_company, step):
    data = ready_company
    candidate, review = _freeze_ready_package(data)
    MonthlyCycle.objects.filter(pk=data["cycle"].pk).update(trenutni_korak=step)

    with pytest.raises(NeveljavenPrehodMesecnegaCikla, match="samo v koraku 5"):
        materializiraj_zamrznjenega_mesecnega_kandidata(
            data["batch"].pk, candidate["lease_id"], review.candidate_fingerprint
        )


def test_conflicting_and_multiple_existing_invoices_fail_closed(ready_company):
    data = ready_company
    candidate, review = _freeze_ready_package(data)
    invoice, _reused = materializiraj_zamrznjenega_mesecnega_kandidata(
        data["batch"].pk, candidate["lease_id"], review.candidate_fingerprint
    )
    invoice.prejemnik_naziv = "Conflicting recipient"
    invoice.save(update_fields=["prejemnik_naziv"])

    with pytest.raises(ValidationError, match="konfliktu"):
        materializiraj_zamrznjenega_mesecnega_kandidata(
            data["batch"].pk, candidate["lease_id"], review.candidate_fingerprint
        )

    Invoice.objects.create(
        company_id=invoice.company_id,
        lease_id=invoice.lease_id,
        document_type=Invoice.DocumentType.NP,
        stevilka="NP-99999",
        datum_izdaje=invoice.datum_izdaje,
        obdobje_od=invoice.obdobje_od,
        obdobje_do=invoice.obdobje_do,
    )
    with pytest.raises(ValidationError, match="več mesečnih računov"):
        materializiraj_zamrznjenega_mesecnega_kandidata(
            data["batch"].pk, candidate["lease_id"], review.candidate_fingerprint
        )
    assert NumberSequence.objects.get().zadnja_stevilka == 1


@pytest.mark.parametrize("invalid", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_frozen_money_fails_before_numbering(ready_company, invalid):
    data = ready_company
    candidate, review = _freeze_ready_package(data)
    snapshot = data["batch"].calculation_snapshot
    candidate = snapshot["candidates"][0]
    candidate["lines"][0]["net"] = invalid
    candidate.pop("candidate_fingerprint")
    fingerprint = _candidate_fingerprint(
        candidate,
        company=snapshot["company"],
        period_start=snapshot["period_start"],
        period_end=snapshot["period_end"],
        issue_date=snapshot["issue_date"],
    )
    candidate["candidate_fingerprint"] = fingerprint
    review.candidate_fingerprint = fingerprint
    review.save(update_fields=["candidate_fingerprint"])
    data["batch"].calculation_snapshot = snapshot
    data["batch"].save(update_fields=["calculation_snapshot"])

    with pytest.raises(ValidationError, match="končno število"):
        materializiraj_zamrznjenega_mesecnega_kandidata(
            data["batch"].pk, candidate["lease_id"], fingerprint
        )
    assert not NumberSequence.objects.exists()
    assert not Invoice.objects.exists()


def test_step5_prg_shows_number_and_conflict_truthfully(ready_company, client):
    data = ready_company
    _candidate, _review = _freeze_ready_package(data)

    response = client.post(
        "/mesecni-obracun/", {"action": "issue_frozen_package"}, follow=True
    )
    assert response.redirect_chain
    assert "Izdano &ndash; NP-00001" in response.content.decode()

    InvoiceLine.objects.update(razlog_rocnega_popravka="Unrelated adjustment")
    content = client.get("/mesecni-obracun/").content.decode()
    assert "Obstoječi mesečni račun je v konfliktu" in content
    assert "Izdano &ndash; NP-00001" not in content


def test_step3_rerun_with_zero_passed_is_truthful(ready_company, client):
    data = ready_company
    preveri_predhodno_kontrolo(data["cycle"])
    client.post("/mesecni-obracun/", {"action": "continue_step3"})
    Company.objects.filter(pk=data["company"].pk).update(ddv_zavezanec=False)

    response = client.post(
        "/mesecni-obracun/", {"action": "rerun_preflight"}, follow=True
    )
    content = response.content.decode()

    assert MonthlyCycle.objects.get().trenutni_korak == 3
    assert "Po ponovnem preverjanju trenutno ni uspešnih podjetij." in content
    assert "zaključena za najmanj eno podjetje" not in content
