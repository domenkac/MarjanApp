from contextlib import ExitStack
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse

from archive.models import ArchivedFile, BackupSnapshot
from catalogs.models import ServiceCatalogItem, VatRegime
from companies.models import Company, NumberSequence, Signatory
from contracts.models import Tenant
from core.validators import preveri_davcno_stevilko
from delivery.models import DeliveryAttempt
from invoicing.models import Invoice, InvoiceLine, MonthlyBatch, MonthlyCycle
from invoicing.services import pripravi_osnutek_st
from tests.business_fixtures import save_business_fixture, save_business_record
from tests.financial_fixtures import save_financial_fixture
from tests.invoicing.test_priprava_st import podatki as podatki  # noqa: PLC0414

pytestmark = pytest.mark.django_db

LIST = "/storitveni-racuni/"
NEW = LIST + "nov/"


def form_data(podatki):
    return {
        "company": podatki["company"].pk,
        "datum_izdaje": "2026-09-01",
        "obdobje_od": "2026-08-01",
        "obdobje_do": "2026-08-31",
        "prejemnik_nacin": "obstojeci",
        "prejemnik": podatki["prejemnik"].pk,
        "storitev": "",
        "opis": "Svetovanje",
        "enota_mere": "ura",
        "vat_regime": podatki["vat_regime"].pk,
        "bruto": "122.00",
    }


def review_url(invoice, company_id=None):
    return reverse("ui:st_review", kwargs={
        "company_id": company_id or invoice.company_id, "invoice_id": invoice.pk,
    })


def additional_company(podatki, index):
    tax = next(
        str(number) for number in range(20000000 + index * 1000, 20001000 + index * 1000)
        if preveri_davcno_stevilko(str(number))
    )
    company = Company.objects.create(
        naziv=f"Izdajatelj {index + 2}", naslov=f"Druga {index + 2}", drzava_koda="SI",
        davcna_stevilka=tax, maticna_stevilka="1234567000", trr=podatki["company"].trr,
    )
    Signatory.objects.create(
        company=company, ime_priimek=f"Podpisnik {index + 2}", funkcija="Direktor",
        podpis_slika="podpis.png", velja_od=date(2026, 1, 1),
    )
    return company


def test_get_form_list_and_navigation(client, podatki, monkeypatch):
    monkeypatch.setattr("ui.views.initial_setup_completed", lambda: True)
    response = client.get(NEW)
    assert response.status_code == 200
    content = response.content.decode()
    for label in (
        "Nov storitveni osnutek", "Drugo — vpiši ročno", "Bruto znesek (EUR)",
        "Referenca listine", "Datum reference", "Shrani osnutek",
    ):
        assert label in content
    assert 'name="csrfmiddlewaretoken"' in content
    assert client.get(LIST).status_code == 200
    dashboard = client.get("/").content.decode()
    assert f'href="{LIST}"' in dashboard
    assert f'href="{NEW}"' in dashboard
    assert "Ročni storitveni računi" in dashboard
    assert not Invoice.objects.exists()


def test_post_taxable_redirects_to_persisted_review(client, podatki):
    response = client.post(NEW, form_data(podatki))
    invoice = Invoice.objects.get()
    assert response.status_code == 302 and response.url == review_url(invoice)
    line = invoice.invoiceline_set.get()
    assert (line.osnova, line.ddv_znesek, line.skupaj) == (
        Decimal("100.00"), Decimal("22.00"), Decimal("122.00"),
    )
    assert (line.opis, line.enota_mere) == ("Svetovanje", "ura")
    assert invoice.prejemnik_id == podatki["prejemnik"].pk
    assert invoice.prejemnik_naziv == "Prejemnik d.o.o."
    assert invoice.prejemnik_polni_odbitek is True
    assert invoice.izdajatelj_naziv == "Alfa d.o.o."
    assert invoice.izdajatelj_trr == podatki["company"].trr
    assert invoice.izdajatelj_podpisnik_ime == "Ana"
    assert (invoice.datum_izdaje, invoice.obdobje_od, invoice.obdobje_do) == (
        date(2026, 9, 1), date(2026, 8, 1), date(2026, 8, 31),
    )
    assert invoice.status == "OSNUTEK" and invoice.stevilka is None
    assert invoice.lease_id is None and invoice.rok_placila is None
    assert invoice.osnova_skupaj is None and invoice.skupaj_z_ddv is None
    before = Invoice.objects.values().get()
    content = client.get(response.url).content.decode()
    for value in (
        "Osnutek — še ni izdan", "Številka ni dodeljena", "Prejemnik d.o.o.",
        "Alfa d.o.o.", "Svetovanje", "ura", "ST_TEST", "100,00", "22,00", "122,00",
        "01. 08. 2026", "31. 08. 2026", "01. 09. 2026", "Ana", "Obdavčeno",
    ):
        assert value in content
    assert Invoice.objects.values().get() == before


def exempt_regime():
    return VatRegime.objects.create(
        sifra="EXEMPT", naziv="Oproščeno", nacin_obracuna="OPROSCENO",
        stopnja=Decimal("0.00"), klavzula="Oproščeno plačila DDV",
        rekapitulacijska_skupina="OPR", zahteva_polni_odbitek=False,
        zahteva_referenco=True, eslog_koda="E", velja_od=date(2026, 1, 1),
    )


def test_exempt_draft_and_reference_review(client, podatki):
    regime = exempt_regime()
    response = client.post(NEW, form_data(podatki) | {
        "vat_regime": regime.pk, "referenca_listine": "IZJAVA-1",
        "datum_reference": "2026-08-01", "bruto": "99,99",
    })
    assert response.status_code == 302
    line = InvoiceLine.objects.get()
    assert (line.osnova, line.ddv_znesek, line.skupaj) == (
        Decimal("99.99"), Decimal("0.00"), Decimal("99.99"),
    )
    assert line.referenca_listine == "IZJAVA-1"
    assert line.datum_reference == date(2026, 8, 1)
    content = client.get(response.url).content.decode()
    assert regime.klavzula in content and "IZJAVA-1" in content
    assert "EXEMPT" in content and "99,99" in content


@pytest.mark.parametrize("override", [False, True])
def test_catalog_defaults_and_explicit_overrides(client, podatki, override):
    item = ServiceCatalogItem.objects.create(
        naziv="Čiščenje", enota_mere="m²", privzeti_ddv_rezim=podatki["vat_regime"],
        privzeta_cena=Decimal("999.00"),
    )
    data = form_data(podatki) | {
        "storitev": item.pk, "opis": "", "enota_mere": "", "vat_regime": "",
    }
    if override:
        data |= {
            "opis": "Posebno čiščenje", "enota_mere": "kos",
            "vat_regime": exempt_regime().pk,
            "referenca_listine": "IZJAVA", "datum_reference": "2026-09-01",
        }
    assert client.post(NEW, data).status_code == 302
    line = InvoiceLine.objects.get()
    assert line.skupaj == Decimal("122.00")
    assert (line.opis, line.enota_mere, line.ddv_rezim_sifra) == (
        ("Posebno čiščenje", "kos", "EXEMPT") if override
        else ("Čiščenje", "m²", "ST_TEST")
    )


def test_new_recipient_is_snapshot_only(client, podatki):
    response = client.post(NEW, form_data(podatki) | {
        "prejemnik_nacin": "nov", "prejemnik": "",
        "novi_tip": "FIZICNA_OSEBA", "novi_naziv": "Nova oseba",
        "novi_naslov": "Nova 1", "novi_drzava_koda": "SI",
    })
    assert response.status_code == 302
    invoice = Invoice.objects.get()
    assert invoice.prejemnik_id is None
    assert invoice.prejemnik_naziv == "Nova oseba"
    assert invoice.prejemnik_naslov == "Nova 1"
    assert invoice.prejemnik_tip == "FIZICNA_OSEBA"
    assert invoice.prejemnik_identificiran_za_ddv is False
    assert invoice.prejemnik_polni_odbitek is False
    assert Tenant.objects.count() == 1


@pytest.mark.parametrize("changes", [
    {"company": ""}, {"company": "999999"}, {"prejemnik": "999999"},
    {"prejemnik": ""}, {"prejemnik_nacin": ""}, {"prejemnik_nacin": "unknown"},
    {"datum_izdaje": ""}, {"datum_izdaje": "2026-02-30"},
    {"obdobje_od": ""}, {"obdobje_do": ""}, {"obdobje_od": "2026-09-02"},
    {"bruto": ""}, {"bruto": "0"}, {"bruto": "-1"}, {"bruto": "NaN"},
    {"bruto": "Infinity"}, {"bruto": "1.000"}, {"bruto": "1.001"},
    {"bruto": "100000000.00"}, {"bruto": "abc"},
    {"opis": " "}, {"enota_mere": ""}, {"vat_regime": ""},
    {"storitev": "999999"}, {"vat_regime": "999999"},
    {"datum_reference": "not-a-date"},
    {"prejemnik_nacin": "nov", "prejemnik": "", "novi_tip": "FIZICNA_OSEBA"},
    {"prejemnik_nacin": "nov", "novi_naziv": "Nov", "novi_tip": "FIZICNA_OSEBA"},
])
def test_invalid_post_preserves_values_without_writes(client, podatki, changes):
    response = client.post(NEW, form_data(podatki) | changes)
    assert response.status_code == 200
    assert response.context["form"].errors
    assert response.context["form"].is_bound
    assert response.context["form"]["bruto"].value() == changes.get("bruto", "122.00")
    assert response.context["form"]["opis"].value() == changes.get("opis", "Svetovanje")
    assert 'role="alert"' in response.content.decode()
    assert "This field is required" not in response.content.decode()
    assert not Invoice.objects.exists() and not InvoiceLine.objects.exists()


@pytest.mark.parametrize("reference,reference_date", [("", ""), ("IZJAVA", ""), ("", "2026-08-01")])
@pytest.mark.parametrize("catalog_default", [False, True])
def test_required_vat_reference_and_date(client, podatki, reference, reference_date, catalog_default):
    regime = exempt_regime()
    data = form_data(podatki) | {
        "vat_regime": regime.pk, "referenca_listine": reference,
        "datum_reference": reference_date,
    }
    if catalog_default:
        item = ServiceCatalogItem.objects.create(
            naziv="Oproščena storitev", enota_mere="kos", privzeti_ddv_rezim=regime,
        )
        data |= {"storitev": item.pk, "vat_regime": ""}
    response = client.post(NEW, data)
    assert response.status_code == 200
    assert "Referenca listine in datum reference sta obvezna" in response.content.decode()
    assert not Invoice.objects.exists()


@pytest.mark.parametrize("target,field,value", [
    ("company", "aktivno", False), ("prejemnik", "aktiven", False),
    ("vat_regime", "aktiven", False),
    ("vat_regime", "velja_od", date(2027, 1, 1)),
    ("prejemnik", "maticna_stevilka", ""),
])
def test_domain_rejections_are_visible(client, podatki, target, field, value):
    record = podatki[target]
    setattr(record, field, value)
    if target == "prejemnik":
        save_business_fixture(record)
    else:
        save_financial_fixture(record)
    response = client.post(NEW, form_data(podatki))
    assert response.status_code == 200 and response.context["form"].errors
    assert not Invoice.objects.exists()


def test_catalog_without_default_and_inactive_catalog(client, podatki):
    item = ServiceCatalogItem.objects.create(naziv="Storitev", enota_mere="kos")
    data = form_data(podatki) | {"storitev": item.pk, "vat_regime": ""}
    assert client.post(NEW, data).context["form"].errors
    item.aktivna = False
    save_business_record(item)
    assert client.post(NEW, data | {"vat_regime": podatki["vat_regime"].pk}).context["form"].errors
    assert not Invoice.objects.exists()


def test_list_distinguishes_drafts_numbers_and_company_scopes(client, podatki):
    first = pripravi_osnutek_st(**podatki)
    numbered = pripravi_osnutek_st(**podatki)
    Invoice.objects.filter(pk=numbered.pk).update(stevilka="ST-2026-0001", status="POSLAN")
    other = additional_company(podatki, 0)
    second = pripravi_osnutek_st(**(podatki | {"company": other, "opis": "Beta storitev"}))
    unrelated = Invoice.objects.create(
        company=other, document_type="NP", datum_izdaje="2026-09-01",
        obdobje_od="2026-08-01", obdobje_do="2026-08-31", stevilka="NP-HIDDEN",
    )
    content = client.get(LIST).content.decode()
    for value in ("Osnutek — še ni izdan", "Številka ni dodeljena", "ST-2026-0001", "Oštevilčen ST", other.naziv):
        assert value in content
    assert review_url(first) in content and review_url(second) in content
    assert review_url(numbered) not in content
    assert "NP-HIDDEN" not in content
    response = client.get(LIST, {"company": other.pk})
    assert list(response.context["invoices"]) == [second]
    assert "Alfa d.o.o." not in response.content.decode().split("<tbody>")[1]
    assert not client.get(LIST, {"company": "invalid"}).context["invoices"]
    assert client.get(review_url(first, other.pk)).status_code == 404
    assert client.get(review_url(numbered)).status_code == 404
    assert client.get(review_url(unrelated)).status_code == 404


def test_five_company_posts_keep_issuer_snapshots_separate(client, podatki):
    companies = [podatki["company"], *(additional_company(podatki, i) for i in range(4))]
    for company in companies:
        response = client.post(NEW, form_data(podatki) | {"company": company.pk})
        assert response.status_code == 302
        invoice = Invoice.objects.get(company=company)
        assert invoice.izdajatelj_naziv == company.naziv
        assert invoice.izdajatelj_davcna_stevilka == company.davcna_stevilka
        assert invoice.izdajatelj_podpisnik_ime == Signatory.objects.get(company=company).ime_priimek
        for other in companies:
            if other != company:
                assert client.get(review_url(invoice, other.pk)).status_code == 404
    assert Invoice.objects.count() == 5
    assert not NumberSequence.objects.exists()


@pytest.mark.parametrize("missing_vat_id", [False, True])
def test_new_foreign_legal_recipient_validation(client, podatki, missing_vat_id):
    response = client.post(NEW, form_data(podatki) | {
        "prejemnik_nacin": "nov", "prejemnik": "", "novi_tip": "PRAVNA_OSEBA",
        "novi_naziv": "Foreign Ltd", "novi_naslov": "Street 1", "novi_drzava_koda": "AT",
        "novi_davcna_stevilka": "123456789", "novi_identificiran_za_ddv": "on",
        "novi_ddv_identifikacijska_stevilka": "" if missing_vat_id else "ATU12345678",
    })
    if missing_vat_id:
        assert response.status_code == 200 and response.context["form"].errors
        assert not Invoice.objects.exists()
    else:
        assert response.status_code == 302
        invoice = Invoice.objects.get()
        assert invoice.prejemnik_identificiran_za_ddv is True
        assert invoice.prejemnik_ddv_identifikacijska_stevilka == "ATU12345678"
        assert invoice.prejemnik_drzava_koda == "AT"
    assert Tenant.objects.count() == 1


def test_missing_signatory_is_slovenian_error_without_writes(client, podatki):
    regime = podatki["vat_regime"]
    regime.velja_od = date(2025, 1, 1)
    save_financial_fixture(regime)
    response = client.post(NEW, form_data(podatki) | {"datum_izdaje": "2025-12-31"})
    assert response.status_code == 200
    assert "Ni veljavnega podpisnika" in response.content.decode()
    assert not Invoice.objects.exists()


def test_late_domain_field_error_rolls_back_and_preserves_input(client, podatki):
    with patch.object(InvoiceLine, "save", side_effect=ValidationError({
        "ddv_stopnja": "Napaka pri shranjevanju.",
    })):
        response = client.post(NEW, form_data(podatki))
    assert response.status_code == 200
    assert "Napaka pri shranjevanju." in response.content.decode()
    assert response.context["form"]["opis"].value() == "Svetovanje"
    assert response.context["form"]["bruto"].value() == "122.00"
    assert not Invoice.objects.exists() and not InvoiceLine.objects.exists()


def test_post_cannot_supply_official_fields(client, podatki):
    response = client.post(NEW, form_data(podatki) | {
        "action": "issue", "stevilka": "ST-2026-9999", "status": "POSLAN",
        "osnova_skupaj": "999.00", "skupaj_z_ddv": "999.00",
        "izdajatelj_naziv": "Forged issuer", "prejemnik_naziv": "Forged recipient",
        "rendered_pdf_path": "forged.pdf",
    })
    assert response.status_code == 302
    invoice = Invoice.objects.get()
    assert invoice.stevilka is None and invoice.status == "OSNUTEK"
    assert invoice.osnova_skupaj is None and invoice.skupaj_z_ddv is None
    assert invoice.izdajatelj_naziv == podatki["company"].naziv
    assert invoice.prejemnik_naziv == podatki["prejemnik"].naziv
    assert not invoice.rendered_pdf_path and not NumberSequence.objects.exists()


def test_review_uses_snapshots_and_escapes_content(client, podatki):
    invoice = pripravi_osnutek_st(**(podatki | {"opis": '<script>alert("x")</script>'}))
    company = podatki["company"]
    company.naziv = "Changed issuer"
    save_financial_fixture(company)
    tenant = podatki["prejemnik"]
    tenant.naziv = "Changed recipient"
    save_business_record(tenant)
    response = client.get(review_url(invoice))
    assert response.status_code == 200
    content = response.content.decode()
    assert "Alfa d.o.o." in content and "Prejemnik d.o.o." in content
    assert "Changed issuer" not in content and "Changed recipient" not in content
    assert "&lt;script&gt;" in content and '<script>alert("x")</script>' not in content


def test_no_operational_effects_and_no_edit_endpoint(client, podatki):
    NumberSequence.objects.create(company=podatki["company"], tip="ST", leto=2026, zadnja_stevilka=3)
    before = list(NumberSequence.objects.values())
    with ExitStack() as stack:
        for target in (
            "invoicing.services.dodeli_naslednjo_stevilko", "invoicing.services.potrdi_osnutek_np",
            "rendering.services.render_pdf", "rendering.services.render_eslog",
            "archive.services.archive_pdf", "delivery.services.prepare_attempt",
            "delivery.services.send_pdf_email", "keyring.get_password", "keyring.set_password",
            "smtplib.SMTP", "smtplib.SMTP_SSL",
        ):
            stack.enter_context(patch(target, side_effect=AssertionError(target)))
        service = stack.enter_context(patch("ui.st_invoices.pripravi_osnutek_st", wraps=pripravi_osnutek_st))
        response = client.post(NEW, form_data(podatki))
        assert response.status_code == 302
        service.assert_called_once()
        invoice = Invoice.objects.get()
        assert client.get(LIST).status_code == 200
        content = client.get(response.url).content.decode()
        for label in ("Potrdi izdajo", "Pošlji", "Arhiviraj", "Uredi osnutek"):
            assert label not in content
        saved = Invoice.objects.values().get()
        for action in ("edit", "issue", "confirm", "send", "archive"):
            assert client.post(response.url, {"action": action}).status_code == 405
        assert client.post(LIST).status_code == 405
        assert client.put(NEW).status_code == 405
    assert saved == Invoice.objects.values().get()
    assert list(NumberSequence.objects.values()) == before
    assert invoice.stevilka is None and invoice.status == "OSNUTEK"
    assert not invoice.rendered_pdf_path and not invoice.checksum
    assert not invoice.rendered_eslog_path and not invoice.dostavni_kontakti_snapshot
    assert not any(model.objects.exists() for model in (
        MonthlyCycle, MonthlyBatch, DeliveryAttempt, ArchivedFile, BackupSnapshot,
    ))


def test_form_keeps_csrf_protection(podatki):
    client = Client(enforce_csrf_checks=True)
    assert client.post(NEW, form_data(podatki)).status_code == 403


def test_empty_post_has_slovenian_required_errors(client):
    response = client.post(NEW, {})
    assert response.status_code == 200
    assert response.context["form"].errors
    assert "To polje je obvezno." in response.content.decode()
    assert not Invoice.objects.exists()
