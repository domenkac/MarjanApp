from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from threading import Barrier
from unittest.mock import patch

import pytest
from django.db import close_old_connections, connection
from django.test import Client
from django.urls import reverse

from catalogs.models import VatRegime
from companies.models import Company, NumberSequence
from core.models import AuditLogEntry
from invoicing import services
from invoicing.models import Invoice
from invoicing.services import (
    dodaj_vrstico,
    fingerprint_osnutka_st,
    potrdi_osnutek_st,
    preglej_osnutek_st,
    pripravi_korekcijski_osnutek,
    pripravi_osnutek_np,
    pripravi_osnutek_st,
    spremeni_podatke_racuna,
    spremeni_vrstico,
)
from rendering.services import render_pdf
from tests.financial_fixtures import save_financial_fixture
from tests.invoicing.test_korekcije import izdan_np
from tests.invoicing.test_priprava_np import pogodba as pogodba  # noqa: PLC0414

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def isolated(settings, tmp_path, monkeypatch):
    settings.RENDERING_ROOT = tmp_path / "rendered"
    monkeypatch.setattr("socket.create_connection", lambda *a, **k: pytest.fail("Network call"))
    for target in ("smtplib.SMTP", "smtplib.SMTP_SSL", "keyring.get_password"):
        monkeypatch.setattr(target, lambda *a, **k: pytest.fail("SMTP call"))


@pytest.fixture
def original(pogodba):
    return izdan_np(pogodba)


def url(name, invoice, **kwargs):
    return reverse(f"ui:correction_{name}", kwargs={
        "company_id": invoice.company_id, "invoice_id": invoice.pk, **kwargs,
    })


def draft(original, kind="STORNO"):
    return pripravi_korekcijski_osnutek(original, kind, "Napačen znesek", date(2027, 1, 2))


def reviewed(client, invoice):
    page = client.get(url("review", invoice))
    assert page.status_code == 200
    response = client.post(url("review", invoice), {
        "token": page.context["form"].initial["token"], "reviewed": "on",
    })
    assert response.status_code == 302
    confirmation = client.get(response.url)
    assert confirmation.status_code == 200
    return confirmation.context["form"].initial["token"]


def line_data(**changes):
    return {
        "opis": "Ročni dobropis", "osnova": "-10.00", "ddv_stopnja": "22.00",
        "ddv_znesek": "-2.20", "skupaj": "-12.20", "ddv_nacin_obracuna": "OBDAVCENO",
        "ddv_eslog_koda": "S", "ddv_rekap_skupina": "OBDAVCENO", **changes,
    }


def test_selection_and_creation(client, original):
    new = reverse("ui:correction_new", args=[original.company_id])
    page = client.get(new)
    assert list(page.context["form"].fields["original"].queryset) == [original]
    before = Invoice.objects.values().get(pk=original.pk)
    old_lines = list(original.invoiceline_set.values())
    response = client.post(new, {
        "original": original.pk, "document_type": "STORNO",
        "datum_izdaje": "2027-01-02", "razlog": "Napačen znesek",
    })
    correction = Invoice.objects.get(povezan_racun=original)
    assert response.status_code == 302 and response.url == url("review", correction)
    assert correction.company_id == original.company_id
    assert correction.prejemnik_naziv == original.prejemnik_naziv
    assert correction.stevilka is None
    assert [v.skupaj for v in correction.invoiceline_set.order_by("pk")] == [
        -v.skupaj for v in original.invoiceline_set.order_by("pk")
    ]
    assert Invoice.objects.values().get(pk=original.pk) == before
    assert list(original.invoiceline_set.values()) == old_lines
    page = client.get(response.url)
    assert original.stevilka in page.content.decode()
    assert "Storno in dobropisi" in page.content.decode()
    assert client.get(url("original", original)).status_code == 200


@pytest.mark.parametrize("reason", ["", "   ", "..."])
def test_reason_required(client, original, reason):
    response = client.post(reverse("ui:correction_new", args=[original.company_id]), {
        "original": original.pk, "document_type": "STORNO",
        "datum_izdaje": "2027-01-02", "razlog": reason,
    })
    assert response.status_code == 200
    assert "razlog" in response.context["form"].errors
    assert Invoice.objects.count() == 1


def test_source_scope_and_types(client, original):
    correction = draft(original)
    new = reverse("ui:correction_new", args=[original.company_id])
    for source_id in (correction.pk, 999999):
        response = client.post(new, {
            "original": source_id, "document_type": "DOBROPIS",
            "datum_izdaje": "2027-01-02", "razlog": "Napaka",
        })
        assert "original" in response.context["form"].errors
    assert client.get(reverse("ui:correction_review", args=[999999, correction.pk])).status_code == 404
    assert client.get(url("review", original)).status_code == 404


def test_storno_no_manual_lines(client, original):
    invoice = draft(original)
    line = invoice.invoiceline_set.first()
    assert client.post(url("line_new", invoice), line_data()).status_code == 404
    assert client.post(url("line_edit", invoice, line_id=line.pk), line_data()).status_code == 404
    assert client.post(url("line_delete", invoice, line_id=line.pk)).status_code == 404


def test_credit_line_management_and_totals(client, original):
    invoice = draft(original, "DOBROPIS")
    new = url("line_new", invoice)
    assert client.post(new, line_data()).status_code == 302
    line = invoice.invoiceline_set.get()
    assert line.osnova == Decimal("-10.00")
    assert client.post(url("line_edit", invoice, line_id=line.pk), line_data(opis="Popravek")).status_code == 302
    page = client.get(url("review", invoice))
    assert page.context["totals"]["skupaj"] == Decimal("-12.20")
    assert client.post(url("line_delete", invoice, line_id=line.pk)).status_code == 302
    assert not invoice.invoiceline_set.exists()
    token = reviewed(client, invoice)
    assert client.post(url("confirm", invoice), {"token": token, "confirm_issue": "on"}).status_code == 409
    invoice.refresh_from_db()
    assert invoice.stevilka is None


@pytest.mark.parametrize("changes", [
    {"osnova": "NaN"}, {"ddv_znesek": "Infinity"}, {"osnova": "-1.001"},
    {"skupaj": "-11.00"}, {"ddv_nacin_obracuna": "INVALID"},
])
def test_invalid_line(client, original, changes):
    invoice = draft(original, "DOBROPIS")
    response = client.post(url("line_new", invoice), line_data(**changes))
    assert response.status_code == 200
    assert response.context["form"].errors
    assert not invoice.invoiceline_set.exists()


def test_stale_line_review_and_confirm(client, original):
    invoice = draft(original, "DOBROPIS")
    client.post(url("line_new", invoice), line_data())
    page = client.get(url("review", invoice))
    token = reviewed(client, invoice)
    spremeni_vrstico(invoice.invoiceline_set.get(), opis="Spremenjen opis")
    assert client.post(url("review", invoice), {
        "token": page.context["form"].initial["token"], "reviewed": "on",
    }).status_code == 409
    assert client.post(url("confirm", invoice), {"token": token, "confirm_issue": "on"}).status_code == 409
    invoice.refresh_from_db()
    assert invoice.stevilka is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("kind", ["STORNO", "DOBROPIS"])
def test_issue_pdf_failure_retry_and_duplicate(client, original, monkeypatch, kind):
    monkeypatch.setitem(connection.settings_dict, "ATOMIC_REQUESTS", True)
    invoice = draft(original, kind)
    if kind == "DOBROPIS":
        assert client.post(url("line_new", invoice), line_data()).status_code == 302
    original_state = Invoice.objects.values().get(pk=original.pk)
    original_lines = list(original.invoiceline_set.order_by("pk").values())
    history = list(AuditLogEntry.objects.order_by("pk").values())
    token = reviewed(client, invoice)
    depth = len(connection.atomic_blocks)

    def fail_pdf(issued):
        assert len(connection.atomic_blocks) == depth
        assert not connection.in_atomic_block
        assert Invoice.objects.get(pk=issued.pk).stevilka == "NP-00002"
        raise RuntimeError("Synthetic PDF failure")

    with patch("ui.corrections.render_pdf", side_effect=fail_pdf):
        response = client.post(url("confirm", invoice), {"token": token, "confirm_issue": "on"})
    assert response.status_code == 302 and "pdf_error=1" in response.url
    invoice.refresh_from_db()
    assert invoice.stevilka == "NP-00002"
    assert client.post(url("confirm", invoice), {"token": token, "confirm_issue": "on"}).status_code == 302
    assert NumberSequence.objects.get(company=original.company, tip="NP").zadnja_stevilka == 2
    assert client.post(url("retry_pdf", invoice)).status_code == 302
    download = client.get(url("pdf", invoice))
    assert download.status_code == 200 and download.content.startswith(b"%PDF-")
    invoice.refresh_from_db()
    checksum = invoice.checksum
    with patch("rendering.services._generiraj_pdf_bytes", side_effect=AssertionError("Overwrite")):
        assert client.post(url("retry_pdf", invoice)).status_code == 302
    invoice.refresh_from_db()
    assert invoice.checksum == checksum
    assert client.get(url("detail", invoice)).status_code == 200
    assert Invoice.objects.values().get(pk=original.pk) == original_state
    assert list(original.invoiceline_set.order_by("pk").values()) == original_lines
    assert list(AuditLogEntry.objects.order_by("pk").values()) == history


def test_csrf(client, original):
    invoice = draft(original)
    secure = Client(enforce_csrf_checks=True)
    for name in ("review", "confirm", "retry_pdf"):
        assert secure.post(url(name, invoice), {}).status_code == 403
    credit = draft(original, "DOBROPIS")
    client.post(url("line_new", credit), line_data())
    line = credit.invoiceline_set.get()
    for name in ("edit", "line_new"):
        assert secure.post(url(name, credit), {}).status_code == 403
    for name in ("line_edit", "line_delete"):
        assert secure.post(url(name, credit, line_id=line.pk), {}).status_code == 403
    assert secure.post(reverse("ui:correction_new", args=[original.company_id]), {}).status_code == 403


def test_drafts_and_other_issuer_rejected(client, original):
    unissued = pripravi_osnutek_np(
        original.lease, date(2026, 10, 1), date(2026, 10, 31), date(2026, 11, 1),
    ).invoice
    other = Company.objects.create(naziv="Drugi izdajatelj", davcna_stevilka="SI-TEST")
    for company_id, source_id in ((original.company_id, unissued.pk), (other.pk, original.pk)):
        page = client.post(reverse("ui:correction_new", args=[company_id]), {
            "original": source_id, "document_type": "STORNO", "datum_izdaje": "2027-01-02",
            "razlog": "Napaka", "company": original.company_id,
        })
        assert "original" in page.context["form"].errors
    invoice = draft(original)
    for name in ("review", "edit", "confirm", "detail", "pdf", "retry_pdf"):
        target = reverse(f"ui:correction_{name}", args=[other.pk, invoice.pk])
        assert client.get(target).status_code in (404, 405)
    assert client.get(reverse("ui:correction_list") + "?company=invalid").status_code == 404


@pytest.mark.parametrize("kind", ["STORNO", "DOBROPIS"])
@pytest.mark.django_db(transaction=True)
def test_standalone_st_correction_end_to_end(client, original, kind):
    st = pripravi_osnutek_st(
        company=original.company, prejemnik=original.lease.tenant,
        vat_regime=original.lease.leaseitem_set.first().vat_regime,
        datum_izdaje=date(2026, 9, 1), obdobje_od=date(2026, 8, 1), obdobje_do=date(2026, 8, 31),
        bruto=Decimal("122.00"), opis="Storitev", enota_mere="ura",
    )
    review = preglej_osnutek_st(st, expected_fingerprint=fingerprint_osnutka_st(st), individual_review=True)
    st = potrdi_osnutek_st(st, pregled=review)
    original_pdf = render_pdf(st)
    before = Invoice.objects.values().get(pk=st.pk)
    source_lines = list(st.invoiceline_set.order_by("pk").values())
    new = reverse("ui:correction_new", args=[st.company_id])
    assert st in client.get(new).context["form"].fields["original"].queryset
    result = client.post(new, {
        "original": st.pk, "document_type": kind, "datum_izdaje": "2027-01-02", "razlog": "Napaka",
    })
    assert result.status_code == 302
    invoice = Invoice.objects.get(povezan_racun=st)
    if kind == "DOBROPIS":
        source_form = client.get(url("line_new", invoice) + f"?source_line={st.invoiceline_set.get().pk}")
        assert client.post(url("line_new", invoice), line_data(
            source_token=source_form.context["form"].initial["source_token"],
        )).status_code == 302
    token = reviewed(client, invoice)
    spremeni_podatke_racuna(invoice, razlog="Popravljen razlog")
    assert client.post(url("confirm", invoice), {"token": token, "confirm_issue": "on"}).status_code == 409
    token = reviewed(client, invoice)
    response = client.post(url("confirm", invoice), {"token": token, "confirm_issue": "on"})
    assert response.status_code == 302, response.content.decode()
    invoice.refresh_from_db()
    assert invoice.stevilka == "ST-2027-0001" and invoice.lease_id is None
    assert invoice.eslog_konfiguracija_snapshot is None
    assert client.get(url("pdf", invoice)).content.startswith(b"%PDF-")
    assert client.get(url("detail", invoice)).status_code == 200
    assert client.post(url("confirm", invoice), {"token": token, "confirm_issue": "on"}).status_code == 302
    assert NumberSequence.objects.get(company=st.company, tip="ST", leto=2027).zadnja_stevilka == 1
    assert Invoice.objects.values().get(pk=st.pk) == before
    assert list(st.invoiceline_set.order_by("pk").values()) == source_lines
    assert render_pdf(st).pdf_bytes == original_pdf.pdf_bytes


@pytest.mark.parametrize("kind", ["STORNO", "DOBROPIS"])
def test_legacy_contract_st_year_numbering(client, original, kind):
    # Existing domain supports historical ST invoices linked to a contract.
    # Never attach a contract to a real standalone ST to bypass its blocker.
    values = Invoice.objects.values().get(pk=original.pk)
    values.pop("id")
    values.update(document_type="ST", stevilka="ST-2026-0001")
    st = Invoice.objects.create(**values)
    NumberSequence.objects.filter(company=st.company, tip="ST", leto=2026).update(zadnja_stevilka=1)
    for source in original.invoiceline_set.all():
        line = {field.attname: getattr(source, field.attname) for field in source._meta.concrete_fields
                if field.name not in {"id", "invoice"}}
        st.invoiceline_set.create(**line)
    invoice = draft(st, kind)
    if kind == "DOBROPIS":
        client.post(url("line_new", invoice), line_data())
    token = reviewed(client, invoice)
    with patch("ui.corrections.render_pdf"):
        response = client.post(url("confirm", invoice), {"token": token, "confirm_issue": "on"})
    assert response.status_code == 302, response.content.decode()
    invoice.refresh_from_db()
    assert invoice.stevilka == "ST-2027-0001"
    assert list(NumberSequence.objects.filter(company=st.company, tip="ST").order_by("leto").values_list(
        "leto", "zadnja_stevilka",
    )) == [(2026, 1), (2027, 1)]
    assert not NumberSequence.objects.filter(tip__in=["STORNO", "DOBROPIS"]).exists()


@pytest.mark.parametrize("changes", [{"ddv_eslog_koda": "E"}, {"ddv_klavzula": "", "ddv_nacin_obracuna": "OPROSCENO", "ddv_eslog_koda": "E"}])
def test_vat_domain_rejects_issuance_without_number(client, original, changes):
    invoice = draft(original, "DOBROPIS")
    client.post(url("line_new", invoice), line_data(**changes))
    token = reviewed(client, invoice)
    assert client.post(url("confirm", invoice), {"token": token, "confirm_issue": "on"}).status_code == 409
    assert NumberSequence.objects.get(company=original.company, tip="NP").zadnja_stevilka == 1


def test_original_prefill_and_foreign_line_ids(client, original):
    invoice = draft(original, "DOBROPIS")
    source = original.invoiceline_set.first()
    page = client.get(url("line_new", invoice) + f"?source_line={source.pk}")
    assert page.context["form"].initial["ddv_eslog_koda"] == source.ddv_eslog_koda
    assert page.context["form"].initial["ddv_klavzula"] == source.ddv_klavzula
    assert "osnova" not in page.context["form"].initial
    for name in ("line_edit", "line_delete"):
        assert client.post(url(name, invoice, line_id=source.pk), line_data()).status_code == 404
    assert client.get(url("line_new", invoice) + "?source_line=invalid").status_code == 404


def test_selected_source_vat_metadata_survives_ui_edits(client, original):
    invoice = draft(original, "DOBROPIS")
    source = original.invoiceline_set.first()
    new = url("line_new", invoice)
    page = client.get(new + f"?source_line={source.pk}")
    token = page.context["form"].initial["source_token"]
    assert client.post(new, line_data(source_token=token, ddv_rezim_sifra="FORGED",
                                     ddv_zahteva_polni_odbitek="True")).status_code == 302
    line = invoice.invoiceline_set.get()
    regime = VatRegime.objects.get(sifra=source.ddv_rezim_sifra)
    regime.stopnja = Decimal("9.50")
    save_financial_fixture(regime, update_fields=["stopnja"])
    assert client.post(url("line_edit", invoice, line_id=line.pk), line_data(
        opis="Urejen opis", ddv_stopnja="9.50", ddv_rezim_sifra="", ddv_zahteva_polni_odbitek="",
    )).status_code == 302
    line.refresh_from_db()
    assert line.opis == "Urejen opis"
    assert line.ddv_rezim_sifra == source.ddv_rezim_sifra
    assert line.ddv_zahteva_polni_odbitek is source.ddv_zahteva_polni_odbitek
    assert line.ddv_stopnja == source.ddv_stopnja


def test_forged_source_id_token_and_rebinding_fail_closed(client, original):
    first, second = draft(original, "DOBROPIS"), draft(original, "DOBROPIS")
    source = original.invoiceline_set.first()
    page = client.get(url("line_new", first) + f"?source_line={source.pk}")
    token = page.context["form"].initial["source_token"]
    assert client.post(url("line_new", first), line_data(source_line=source.pk)).status_code == 409
    assert client.post(url("line_new", first), line_data(source_token=token + "x")).status_code == 409
    assert client.post(url("line_new", second), line_data(source_token=token)).status_code == 409
    assert not first.invoiceline_set.exists() and not second.invoiceline_set.exists()
    assert client.post(url("line_new", first), line_data(source_token=token)).status_code == 302
    line = first.invoiceline_set.get()
    assert client.post(url("line_edit", first, line_id=line.pk), line_data(source_token=token)).status_code == 409
    assert client.get(url("line_new", first) + f"?source_line={line.pk}").status_code == 404


@pytest.mark.parametrize("change", ["reason", "date", "delete", "add"])
def test_stale_other_state(client, original, change):
    invoice = draft(original, "DOBROPIS")
    line = dodaj_vrstico(invoice, **{k: Decimal(v) if k in {"osnova", "ddv_stopnja", "ddv_znesek", "skupaj"} else v for k, v in line_data().items()})
    token = reviewed(client, invoice)
    if change == "reason":
        spremeni_podatke_racuna(invoice, razlog="Nov razlog")
    elif change == "date":
        spremeni_podatke_racuna(invoice, datum_izdaje=date(2027, 1, 3))
    elif change == "delete":
        services.izbrisi_vrstico(line)
    else:
        client.post(url("line_new", invoice), line_data())
    assert client.post(url("confirm", invoice), {"token": token, "confirm_issue": "on"}).status_code == 409


def test_token_tampering_cross_document_and_missing_consent(client, original):
    first, second = draft(original), draft(original)
    token = reviewed(client, first)
    assert client.post(url("confirm", second), {"token": token, "confirm_issue": "on"}).status_code == 409
    assert client.post(url("confirm", first), {"token": token + "x", "confirm_issue": "on"}).status_code == 409
    assert client.post(url("confirm", first), {"token": token}).status_code == 400
    page = client.get(url("review", first))
    assert client.post(url("confirm", first), {
        "token": page.context["form"].initial["token"], "confirm_issue": "on",
    }).status_code == 409


def test_change_during_asset_preparation_rejected(client, original):
    invoice = draft(original, "DOBROPIS")
    client.post(url("line_new", invoice), line_data())
    token = reviewed(client, invoice)
    save_asset = services.shrani_nespremenljiv_asset
    depth = len(connection.atomic_blocks)

    def mutate(path):
        assert len(connection.atomic_blocks) == depth
        spremeni_vrstico(invoice.invoiceline_set.get(), opis="Spremenjeno med potrditvijo")
        return save_asset(path)

    with patch("invoicing.services.shrani_nespremenljiv_asset", side_effect=mutate):
        assert client.post(url("confirm", invoice), {"token": token, "confirm_issue": "on"}).status_code == 409
    invoice.refresh_from_db()
    assert invoice.stevilka is None


@pytest.mark.django_db(transaction=True)
def test_concurrent_confirmation_allocates_once(client, original):
    invoice = draft(original)
    token = reviewed(client, invoice)
    barrier = Barrier(2)

    def confirm():
        close_old_connections()
        try:
            barrier.wait(timeout=20)
            return Client().post(url("confirm", invoice), {"token": token, "confirm_issue": "on"}).status_code
        finally:
            close_old_connections()

    with patch("ui.corrections.render_pdf") as render, ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _: confirm(), range(2)))
    assert 302 in statuses and all(status in (302, 409) for status in statuses)
    render.assert_called_once()
    assert NumberSequence.objects.get(company=original.company, tip="NP").zadnja_stevilka == 2


def test_verified_pdf_damage_never_overwritten(client, original, settings):
    invoice = draft(original)
    token = reviewed(client, invoice)
    assert client.post(url("confirm", invoice), {"token": token, "confirm_issue": "on"}).status_code == 302
    invoice.refresh_from_db()
    path = settings.RENDERING_ROOT / invoice.rendered_pdf_path
    checksum = invoice.checksum
    path.write_bytes(b"damaged synthetic PDF")
    assert client.get(url("pdf", invoice)).status_code == 409
    assert "pdf_error=1" in client.post(url("retry_pdf", invoice)).url
    assert path.read_bytes() == b"damaged synthetic PDF"
    invoice.refresh_from_db()
    assert invoice.checksum == checksum and invoice.stevilka == "NP-00002"
