from contextlib import ExitStack
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.db.models.deletion import ProtectedError

from archive.models import ArchivedFile, BackupSnapshot
from catalogs.models import ServiceCatalogItem, VatRegime
from companies.models import Company, NumberSequence, Signatory
from contracts.models import Lease, LeaseItem, Tenant
from contracts.services import fizicno_izbrisi_poslovni_zapis, odloci_o_fizicnem_izbrisu
from invoicing.models import Invoice, InvoiceLine, MonthlyBatch, MonthlyCycle
from invoicing.services import (
    dodaj_vrstico,
    izbrisi_racun,
    izbrisi_vrstico,
    pripravi_osnutek_st,
    spremeni_podatke_racuna,
    spremeni_vrstico,
)
from tests.business_fixtures import save_business_record
from tests.financial_fixtures import save_financial_fixture

DATUM = date(2026, 9, 1)


@pytest.fixture
def podatki(db):
    company = Company.objects.create(
        naziv="Alfa d.o.o.", naslov="Glavna 1", drzava_koda="SI",
        davcna_stevilka="15012557", maticna_stevilka="1234567000",
        trr="SI56191000000123438",
    )
    Signatory.objects.create(
        company=company, ime_priimek="Ana", funkcija="Direktorica",
        podpis_slika="podpis.png", velja_od=date(2026, 1, 1),
    )
    tenant = Tenant.objects.create(
        tip=Tenant.Tip.PRAVNA_OSEBA, naziv="Prejemnik d.o.o.", naslov="Trg 1",
        drzava_koda="SI", davcna_stevilka="15012557",
        maticna_stevilka="1234567000", identificiran_za_ddv=True,
        ddv_zavezanec_s_polnim_odbitkom=True, email="prejemnik@example.com",
    )
    regime = VatRegime.objects.create(
        sifra="ST_TEST", naziv="Storitev", nacin_obracuna="OBDAVCENO",
        stopnja=Decimal("22.00"), rekapitulacijska_skupina="OBD_22",
        zahteva_polni_odbitek=False, eslog_koda="S", velja_od=date(2026, 1, 1),
    )
    return {
        "company": company, "prejemnik": tenant, "vat_regime": regime,
        "datum_izdaje": DATUM, "obdobje_od": date(2026, 8, 1),
        "obdobje_do": date(2026, 8, 31), "bruto": Decimal("122.00"),
        "opis": "Svetovanje", "enota_mere": "ura",
    }


def test_taxable_draft_without_lease(podatki):
    invoice = pripravi_osnutek_st(**podatki)
    invoice.refresh_from_db()
    line = invoice.invoiceline_set.get()
    assert invoice.document_type == "ST"
    assert invoice.status == "OSNUTEK"
    assert invoice.stevilka is None
    assert invoice.lease_id is None
    assert invoice.oznaka_pogodbe is None
    assert invoice.rok_placila is None
    assert invoice.osnova_skupaj is None
    assert (line.osnova, line.ddv_znesek, line.skupaj) == (
        Decimal("100.00"), Decimal("22.00"), Decimal("122.00"),
    )
    assert line.enota_mere == "ura"
    assert line.lease_id is None and line.lease_item_id is None
    assert invoice.prejemnik_polni_odbitek is True
    assert invoice.izdajatelj_podpisnik_ime == "Ana"
    assert not Lease.objects.exists() and not LeaseItem.objects.exists()


@pytest.mark.parametrize("gross", ["0.01", "0.03", "1.00", "10.01", "99999999.99"])
def test_exact_cent_equality_and_shared_gross_calculation(podatki, gross):
    from core.pravila import izracunaj_iz_bruto

    podatki["bruto"] = Decimal(gross)
    with patch("invoicing.services.izracunaj_iz_bruto", wraps=izracunaj_iz_bruto) as calc:
        line = pripravi_osnutek_st(**podatki).invoiceline_set.get()
    calc.assert_called_once_with(Decimal(gross), Decimal("22.00"))
    assert line.osnova + line.ddv_znesek == line.skupaj == Decimal(gross)


@pytest.mark.parametrize("mode,code", [
    ("OPROSCENO", "E"), ("NI_PREDMET", "O"), ("OBRNJENA_DAVCNA", "AE"),
])
def test_non_taxable_modes_ignore_nominal_rate(podatki, mode, code):
    regime = podatki["vat_regime"]
    regime = VatRegime.objects.create(
        sifra=mode, naziv=mode, nacin_obracuna=mode, stopnja=regime.stopnja,
        rekapitulacijska_skupina=mode, klavzula="Davčna klavzula",
        zahteva_polni_odbitek=False, eslog_koda=code, velja_od=date(2026, 1, 1),
    )
    line = pripravi_osnutek_st(**(podatki | {"vat_regime": regime})).invoiceline_set.get()
    assert line.osnova == line.skupaj == Decimal("122.00")
    assert line.ddv_znesek == Decimal("0.00")
    assert line.ddv_stopnja == Decimal("22.00")
    assert line.ddv_nacin_obracuna == mode
    assert line.ddv_klavzula == "Davčna klavzula"
    assert line.ddv_eslog_koda == code


@pytest.mark.parametrize("gross", [
    Decimal("1.001"), Decimal("1.000"), Decimal("-0.01"), Decimal("0.00"),
    Decimal("100000000.00"), Decimal("NaN"), Decimal("sNaN"),
    Decimal("Infinity"), Decimal("-Infinity"), 1.22, "122.00", None, True,
])
def test_invalid_gross_is_rejected_without_writes(podatki, gross):
    with pytest.raises(ValidationError):
        pripravi_osnutek_st(**(podatki | {"bruto": gross}))
    assert not Invoice.objects.exists() and not InvoiceLine.objects.exists()


def test_catalog_defaults_and_explicit_vat_override(podatki):
    catalog = ServiceCatalogItem.objects.create(
        naziv="Čiščenje", privzeti_ddv_rezim=podatki["vat_regime"],
        enota_mere="m²", privzeta_cena=Decimal("999.00"),
    )
    arguments = {k: v for k, v in podatki.items() if k not in {"opis", "enota_mere", "vat_regime"}}
    line = pripravi_osnutek_st(**arguments, storitev=catalog).invoiceline_set.get()
    assert (line.opis, line.enota_mere, line.skupaj) == ("Čiščenje", "m²", Decimal("122.00"))
    exempt = VatRegime.objects.create(
        sifra="EXEMPT", naziv="Oproščeno", nacin_obracuna="OPROSCENO",
        stopnja=Decimal("0.00"), klavzula="Oproščeno", rekapitulacijska_skupina="OPR",
        zahteva_polni_odbitek=False, eslog_koda="E", velja_od=date(2026, 1, 1),
    )
    line = pripravi_osnutek_st(
        **arguments, storitev=catalog, vat_regime=exempt,
    ).invoiceline_set.get()
    assert line.ddv_nacin_obracuna == "OPROSCENO"
    assert line.ddv_znesek == Decimal("0.00")
    catalog.naziv = "Spremenjena storitev"
    catalog.enota_mere = "ura"
    save_business_record(catalog)
    line.refresh_from_db()
    assert (line.opis, line.enota_mere) == ("Čiščenje", "m²")


def test_other_description_needs_explicit_vat_and_unit(podatki):
    for changes in ({"opis": " "}, {"vat_regime": None}, {"enota_mere": " "}):
        with pytest.raises(ValidationError):
            pripravi_osnutek_st(**(podatki | changes))
    assert not Invoice.objects.exists()


def test_new_recipient_is_validated_and_snapshotted_without_master_write(podatki):
    fields = {
        "tip": "FIZICNA_OSEBA", "naziv": "Nova oseba", "naslov": "Nova 1",
        "drzava_koda": "SI", "identificiran_za_ddv": False,
        "ddv_zavezanec_s_polnim_odbitkom": False,
    }
    arguments = {k: v for k, v in podatki.items() if k != "prejemnik"}
    invoice = pripravi_osnutek_st(**arguments, prejemnik_podatki=fields)
    assert invoice.prejemnik_naziv == "Nova oseba"
    assert invoice.prejemnik_tip == "FIZICNA_OSEBA"
    assert invoice.prejemnik_identificiran_za_ddv is False
    assert invoice.prejemnik_polni_odbitek is False
    assert Tenant.objects.count() == 1
    for changes in ({"naziv": " "}, {"tip": "PRAVNA_OSEBA"},
                    {"identificiran_za_ddv": "False"}, {"id": 99}):
        with pytest.raises(ValidationError):
            pripravi_osnutek_st(**arguments, prejemnik_podatki=fields | changes)
    with pytest.raises(ValidationError):
        pripravi_osnutek_st(**podatki, prejemnik_podatki=fields)


@pytest.mark.parametrize("target,field,value", [
    ("company", "naziv", " "), ("company", "trr", "invalid"),
    ("company", "aktivno", False), ("prejemnik", "aktiven", False),
    ("prejemnik", "davcna_stevilka", "12345678"),
    ("prejemnik", "maticna_stevilka", ""),
])
def test_issuer_and_recipient_use_fresh_validated_records(podatki, target, field, value):
    record = type(podatki[target]).objects.get(pk=podatki[target].pk)
    setattr(record, field, value)
    if target == "company":
        save_financial_fixture(record)
    else:
        from tests.business_fixtures import save_business_fixture

        save_business_fixture(record)
    with pytest.raises(ValidationError):
        pripravi_osnutek_st(**podatki)
    assert not Invoice.objects.exists()


def test_eligibility_and_required_reference(podatki):
    regime = podatki["vat_regime"]
    regime.zahteva_polni_odbitek = True
    regime.zahteva_referenco = True
    save_financial_fixture(regime)
    with pytest.raises(ValidationError, match="[Rr]eferenc"):
        pripravi_osnutek_st(**podatki)
    arguments = podatki | {"referenca_listine": "IZJAVA-1", "datum_reference": DATUM}
    invoice = pripravi_osnutek_st(**arguments)
    line = invoice.invoiceline_set.get()
    assert line.referenca_listine == "IZJAVA-1" and line.datum_reference == DATUM
    tenant = podatki["prejemnik"]
    tenant.ddv_zavezanec_s_polnim_odbitkom = False
    save_business_record(tenant)
    with pytest.raises(ValidationError, match="polno pravico"):
        pripravi_osnutek_st(**arguments)
    assert Invoice.objects.count() == 1


def test_reference_pair_is_required_and_late_validation_rolls_back(podatki):
    regime = podatki["vat_regime"]
    regime.zahteva_referenco = True
    save_financial_fixture(regime)
    for changes in (
        {"referenca_listine": "IZJAVA-1"}, {"datum_reference": DATUM},
        {"referenca_listine": " ", "datum_reference": DATUM},
        {"referenca_listine": "IZJAVA-1", "datum_reference": "2026-09-01"},
    ):
        with pytest.raises(ValidationError):
            pripravi_osnutek_st(**(podatki | changes))
    with (
        patch.object(InvoiceLine, "save", side_effect=ValidationError("rollback")),
        pytest.raises(ValidationError, match="rollback"),
    ):
        pripravi_osnutek_st(**(podatki | {
            "referenca_listine": "IZJAVA-1", "datum_reference": DATUM,
        }))
    assert not Invoice.objects.exists() and not InvoiceLine.objects.exists()


def test_catalog_without_default_requires_selection_and_inactive_catalog_is_rejected(podatki):
    catalog = ServiceCatalogItem.objects.create(naziv="Storitev", enota_mere="kos")
    arguments = podatki | {"storitev": catalog, "vat_regime": None}
    with pytest.raises(ValidationError, match="DDV režim je obvezen"):
        pripravi_osnutek_st(**arguments)
    from tests.business_fixtures import save_business_fixture

    catalog.aktivna = False
    save_business_fixture(catalog)
    with pytest.raises(ValidationError, match="Storitev ni aktivna"):
        pripravi_osnutek_st(**(arguments | {"vat_regime": podatki["vat_regime"]}))


def test_exemption_clause_and_signatory_are_required(podatki):
    regime = VatRegime.objects.create(
        sifra="BAD_EXEMPT", naziv="Oproščeno", nacin_obracuna="OPROSCENO",
        stopnja=Decimal("0.00"), rekapitulacijska_skupina="OPR",
        zahteva_polni_odbitek=False, eslog_koda="E", velja_od=date(2026, 1, 1),
    )
    with pytest.raises(ValidationError, match="Klavzula"):
        pripravi_osnutek_st(**(podatki | {"vat_regime": regime}))
    Signatory.objects.all().delete()
    with pytest.raises(ValueError, match="podpisnika"):
        pripravi_osnutek_st(**podatki)
    assert not Invoice.objects.exists()


def test_foreign_recipient_vat_identity_is_preserved(podatki):
    arguments = podatki | {
        "prejemnik": None,
        "prejemnik_podatki": {
            "tip": "PRAVNA_OSEBA", "naziv": "Foreign Ltd", "naslov": "Street 1",
            "drzava_koda": "AT", "davcna_stevilka": "123456789",
            "identificiran_za_ddv": True, "ddv_identifikacijska_stevilka": "ATU12345678",
            "ddv_zavezanec_s_polnim_odbitkom": False,
        },
    }
    invoice = pripravi_osnutek_st(**arguments)
    assert invoice.prejemnik_drzava_koda == "AT"
    assert invoice.prejemnik_ddv_identifikacijska_stevilka == "ATU12345678"
    arguments["prejemnik_podatki"]["ddv_identifikacijska_stevilka"] = ""
    with pytest.raises(ValidationError):
        pripravi_osnutek_st(**arguments)


def test_st_recipient_link_preserves_deletion_protection(podatki):
    invoice = pripravi_osnutek_st(**podatki)
    recipient = podatki["prejemnik"]
    assert invoice.prejemnik_id == recipient.pk
    decision = odloci_o_fizicnem_izbrisu(recipient, izrecna_potrditev=True)
    assert not decision.dovoljeno
    assert "POVEZAN_RACUN" in {reason.koda for reason in decision.razlogi}
    assert not fizicno_izbrisi_poslovni_zapis(recipient, izrecna_potrditev=True).dovoljeno
    with pytest.raises(ProtectedError):
        recipient.delete()
    assert Tenant.objects.filter(pk=recipient.pk).exists()


@pytest.mark.parametrize("changes", [
    {"obdobje_od": date(2026, 9, 1)}, {"datum_izdaje": "2026-09-01"},
])
def test_explicit_valid_dates_required(podatki, changes):
    with pytest.raises(ValidationError):
        pripravi_osnutek_st(**(podatki | changes))
    assert not Invoice.objects.exists()


@pytest.mark.parametrize("field,value", [
    ("aktiven", False), ("stopnja", Decimal("-1.00")),
    ("zahteva_polni_odbitek", None), ("eslog_koda", "E"),
    ("velja_od", date(2027, 1, 1)), ("velja_do", date(2025, 1, 1)),
    ("rekapitulacijska_skupina", " "),
])
def test_invalid_vat_is_rejected(podatki, field, value):
    regime = podatki["vat_regime"]
    setattr(regime, field, value)
    save_financial_fixture(regime)
    with pytest.raises((ValidationError, ValueError)):
        pripravi_osnutek_st(**podatki)
    assert not Invoice.objects.exists()


def test_historical_snapshots_survive_master_edits(podatki):
    invoice = pripravi_osnutek_st(**podatki)
    before = InvoiceLine.objects.filter(invoice=invoice).values().get()
    regime = podatki["vat_regime"]
    regime.stopnja = Decimal("9.50")
    regime.klavzula = "Nova klavzula"
    save_financial_fixture(regime)
    tenant = podatki["prejemnik"]
    tenant.naziv = "Spremenjen prejemnik"
    save_business_record(tenant)
    company = podatki["company"]
    company.naziv = "Spremenjen izdajatelj"
    save_financial_fixture(company)
    invoice.refresh_from_db()
    assert InvoiceLine.objects.filter(invoice=invoice).values().get() == before
    assert invoice.prejemnik_naziv == "Prejemnik d.o.o."
    assert invoice.izdajatelj_naziv == "Alfa d.o.o."


def test_no_official_or_monthly_side_effects_and_no_numbers(podatki):
    from delivery.models import DeliveryAttempt

    NumberSequence.objects.create(company=podatki["company"], tip="NP", leto=0, zadnja_stevilka=8)
    NumberSequence.objects.create(company=podatki["company"], tip="ST", leto=2026, zadnja_stevilka=3)
    before = list(NumberSequence.objects.values())
    with ExitStack() as stack:
        for target in (
            "invoicing.services.dodeli_naslednjo_stevilko", "rendering.services.render_pdf",
            "rendering.services.render_eslog", "archive.services.archive_pdf",
            "delivery.services.prepare_attempt", "delivery.services.send_pdf_email",
            "keyring.get_password", "keyring.set_password", "smtplib.SMTP", "smtplib.SMTP_SSL",
        ):
            stack.enter_context(patch(target, side_effect=AssertionError(target)))
        first = pripravi_osnutek_st(**podatki)
        second = pripravi_osnutek_st(**podatki)
        with pytest.raises(ValueError, match="ST"):
            spremeni_vrstico(first.invoiceline_set.get(), opis="Nov opis")
        with pytest.raises(ValueError, match="ST"):
            spremeni_podatke_racuna(first, datum_izdaje=date(2026, 9, 2))
    assert first.pk != second.pk
    assert list(NumberSequence.objects.values()) == before
    assert not any(model.objects.exists() for model in (
        MonthlyCycle, MonthlyBatch, DeliveryAttempt, ArchivedFile, BackupSnapshot,
    ))
    for invoice in Invoice.objects.all():
        assert invoice.stevilka is None
        assert not invoice.rendered_pdf_path and not invoice.checksum
        assert not invoice.rendered_eslog_path and not invoice.dostavni_kontakti_snapshot


def test_repeated_edits_with_stale_objects_never_mutate_issued_invoice(podatki):
    draft = pripravi_osnutek_st(**podatki)
    line = draft.invoiceline_set.get()
    Invoice.objects.filter(pk=draft.pk).update(stevilka="ST-2026-0001", status="POSLAN")
    before = Invoice.objects.filter(pk=draft.pk).values().get()
    line_before = InvoiceLine.objects.filter(pk=line.pk).values().get()
    for _ in range(2):
        for action in (
            lambda: spremeni_podatke_racuna(draft, prejemnik_naziv="Drug"),
            lambda: spremeni_vrstico(line, osnova=Decimal("1.00")),
            lambda: dodaj_vrstico(draft, opis="Drug"),
            lambda: izbrisi_vrstico(line), lambda: izbrisi_racun(draft),
        ):
            with pytest.raises(ValueError):
                action()
        pripravi_osnutek_st(**podatki)
    assert Invoice.objects.filter(pk=draft.pk).values().get() == before
    assert InvoiceLine.objects.filter(pk=line.pk).values().get() == line_before


def _st_state(invoice):
    return (
        Invoice.objects.filter(pk=invoice.pk).values().get(),
        list(InvoiceLine.objects.filter(invoice=invoice).order_by("pk").values()),
        list(NumberSequence.objects.order_by("pk").values()),
    )


def _referenced_st(podatki):
    regime = podatki["vat_regime"]
    regime.zahteva_referenco = True
    save_financial_fixture(regime)
    return pripravi_osnutek_st(
        **podatki, referenca_listine="IZJAVA-1", datum_reference=DATUM,
    )


@pytest.mark.parametrize("changes", [
    {"skupaj": Decimal("999.00")}, {"osnova": Decimal("-1.00")},
    {"ddv_znesek": Decimal("0.00")}, {"ddv_stopnja": Decimal("9.50")},
    {"ddv_nacin_obracuna": "NI_PREDMET"}, {"ddv_rezim_sifra": "OTHER"},
    {"ddv_zahteva_polni_odbitek": False}, {"ddv_klavzula": ""},
    {"referenca_listine": ""}, {"datum_reference": None},
    {"opis": "Nov opis"}, {"enota_mere": "kos"},
])
@pytest.mark.parametrize("stale", [False, True])
def test_generic_st_line_edits_fail_closed_without_changing_snapshots(podatki, changes, stale):
    invoice = _referenced_st(podatki)
    line = invoice.invoiceline_set.get()
    before = _st_state(invoice)
    if stale:
        invoice.document_type = "NP"
        line.invoice = invoice
    with pytest.raises(ValueError, match="ST"):
        spremeni_vrstico(line, **changes)
    assert _st_state(invoice) == before
    line.refresh_from_db()
    assert line.skupaj == line.osnova + line.ddv_znesek
    assert line.referenca_listine == "IZJAVA-1" and line.datum_reference == DATUM


@pytest.mark.parametrize("changes", [
    {"document_type": "NP"}, {"document_type": "DOBROPIS"},
    {"datum_izdaje": date(2027, 1, 1)}, {"prejemnik_naziv": "Drug prejemnik"},
    {"izdajatelj_naziv": "Drug izdajatelj"}, {"obdobje_do": date(2027, 1, 1)},
    {"osnova_skupaj": Decimal("999.00")},
])
@pytest.mark.parametrize("stale", [False, True])
def test_generic_st_invoice_edits_fail_closed_using_persisted_type(podatki, changes, stale):
    invoice = _referenced_st(podatki)
    before = _st_state(invoice)
    if stale:
        invoice.document_type = "NP"
    with pytest.raises(ValueError, match="ST"):
        spremeni_podatke_racuna(invoice, **changes)
    assert _st_state(invoice) == before
    invoice.refresh_from_db()
    assert invoice.document_type == "ST"
    line = invoice.invoiceline_set.get()
    assert line.skupaj == line.osnova + line.ddv_znesek


@pytest.mark.parametrize("changes", [
    {}, {"skupaj": Decimal("999.00")}, {"skupaj": Decimal("-122.00")},
    {"skupaj": Decimal("1.001")}, {"skupaj": 1.22},
    {"ddv_znesek": Decimal("0.00")}, {"referenca_listine": "", "datum_reference": None},
])
def test_generic_st_line_additions_are_rejected_atomically(podatki, changes):
    invoice = _referenced_st(podatki)
    before = _st_state(invoice)
    line_fields = before[1][0].copy()
    del line_fields["id"]
    del line_fields["invoice_id"]
    with pytest.raises(ValueError, match="ST"):
        dodaj_vrstico(invoice, **(line_fields | changes))
    assert _st_state(invoice) == before
    line = invoice.invoiceline_set.get()
    assert line.skupaj == line.osnova + line.ddv_znesek


def test_generic_st_line_deletion_cannot_leave_empty_draft(podatki):
    invoice = _referenced_st(podatki)
    before = _st_state(invoice)
    with pytest.raises(ValueError, match="ST"):
        izbrisi_vrstico(invoice.invoiceline_set.get())
    assert _st_state(invoice) == before


def test_generic_draft_cannot_be_converted_into_st(podatki):
    invoice = Invoice.objects.create(
        company=podatki["company"], document_type="NP", datum_izdaje=DATUM,
        obdobje_od=podatki["obdobje_od"], obdobje_do=podatki["obdobje_do"],
    )
    before = _st_state(invoice)
    with pytest.raises(ValueError, match="ST"):
        spremeni_podatke_racuna(invoice, document_type="ST")
    assert _st_state(invoice) == before
