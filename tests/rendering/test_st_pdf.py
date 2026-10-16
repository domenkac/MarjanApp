"""Official standalone ST rendering through the shared PDF pipeline."""
# ruff: noqa: F811

import json
from contextlib import ExitStack
from dataclasses import replace
from datetime import date
from decimal import Decimal
from hashlib import sha256
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.db import models, transaction

from archive.models import ArchivedFile, BackupSnapshot
from companies.models import Company, CompanySampleConfirmation
from companies.services import veljavna_potrditev_vzorca
from core.models import AuditLogEntry
from delivery.models import DeliveryAttempt
from invoicing.models import Invoice
from invoicing.services import potrdi_osnutek_st
from rendering.contexts import PdfRenderNapaka, pripravi_pdf_context
from rendering.services import pripravi_html, render_pdf, stanje_koncnega_pdf
from tests.financial_fixtures import save_financial_fixture
from tests.invoicing.test_potrditev_st import (  # noqa: F401
    counters,
    draft,
    ready,
    review,
    setup_data,
)

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def confirmed(ready):
    invoice = draft(ready)
    return potrdi_osnutek_st(invoice, pregled=review(invoice))


def corrupt(invoice, **changes):
    # Bypass domain guards only to represent corrupt historical storage.
    models.QuerySet.update(Invoice.objects.filter(pk=invoice.pk), **changes)


def test_confirmed_st_uses_shared_pipeline_without_lease(confirmed, settings):
    before = counters()
    samples = list(CompanySampleConfirmation.objects.values())
    assert confirmed.lease_id is None
    line = confirmed.invoiceline_set.get()
    assert line.lease_id is None and line.lease_item_id is None
    with ExitStack() as stack:
        for name in (
            "smtplib.SMTP", "smtplib.SMTP_SSL", "keyring.get_password",
            "invoicing.services._razresi_eslog_snapshot",
            "invoicing.services.razresi_eslog_konfiguracijo",
            "companies.services.dodeli_naslednjo_stevilko",
            "archive.configuration.validate_archive_locations",
        ):
            stack.enter_context(patch(name, side_effect=AssertionError(name)))
        result = render_pdf(confirmed)
    context = pripravi_pdf_context(confirmed)
    html = pripravi_html(context)
    assert result.pdf_bytes.startswith(b"%PDF-")
    assert result.pdf_bytes.rstrip().endswith(b"%%EOF")
    assert result.pot == f"company-{confirmed.company_id}/ST-2026-0001.pdf"
    assert result.checksum == sha256(result.pdf_bytes).hexdigest()
    assert (settings.RENDERING_ROOT / result.pot).read_bytes() == result.pdf_bytes
    assert context.pdf_template_version == settings.ST_PDF_TEMPLATE_VERSION
    assert settings.PDF_TEMPLATE_VERSION == "1"
    for value in (
        "ST-2026-0001", confirmed.izdajatelj_naziv, confirmed.prejemnik_naziv,
        "01.10.2026", "01.09.2026", "30.09.2026", "16.10.2026",
        "Svetovanje", "ura", "100,00", "22,00", "122,00",
        confirmed.izdajatelj_podpisnik_ime, "data:image/png;base64,",
    ):
        assert value in html
    assert (context.osnova_skupaj, context.ddv_skupaj, context.skupaj_z_ddv) == (
        Decimal("100.00"), Decimal("22.00"), Decimal("122.00"),
    )
    assert (context.vrstice[0].osnova, context.vrstice[0].ddv_znesek,
            context.vrstice[0].skupaj) == (line.osnova, line.ddv_znesek, line.skupaj)
    assert context.oznaka_pogodbe is None and context.objekt_naziv is None
    assert stanje_koncnega_pdf(confirmed)["state"] == "READY"
    assert counters() == before
    assert list(CompanySampleConfirmation.objects.values()) == samples
    assert Company.objects.count() == 5
    assert all(veljavna_potrditev_vzorca(company).stanje == "valid" for company in Company.objects.all())
    assert not settings.FEATURE_ESLOG
    assert not DeliveryAttempt.objects.exists()
    assert not ArchivedFile.objects.exists() and not BackupSnapshot.objects.exists()


@pytest.mark.parametrize("mode", ["OPROSCENO", "NI_PREDMET", "OBRNJENA_DAVCNA"])
def test_exempt_st_displays_snapshot_unit_reference_date_and_clause(ready, mode):
    regime = ready.regime
    regime.nacin_obracuna = mode
    regime.stopnja = Decimal("22.00")
    regime.eslog_koda = {"OPROSCENO": "E", "NI_PREDMET": "O", "OBRNJENA_DAVCNA": "AE"}[mode]
    regime.klavzula = "Shranjena DDV klavzula"
    regime.zahteva_referenco = True
    save_financial_fixture(regime)
    # The setup sample reflects the revised VAT regime.
    from companies.services import potrdi_ddv_nastavitve
    from tests.setup_helpers import approve_sample
    ready.item.referenca_listine = "LISTINA 42"
    ready.item.datum_reference = date(2026, 8, 15)
    save_financial_fixture(ready.item)
    potrdi_ddv_nastavitve(ready.company)
    approve_sample(ready.company, ready.lease)
    invoice = draft(ready, bruto=Decimal("0.03"), referenca_listine="LISTINA <42>",
                    datum_reference=date(2026, 8, 15))
    invoice = potrdi_osnutek_st(invoice, pregled=review(invoice))
    context = pripravi_pdf_context(invoice)
    html = pripravi_html(context)
    assert "LISTINA &lt;42&gt;" in html and "15.08.2026" in html
    assert "ura" in html and "Shranjena DDV klavzula" in html
    treatment = {"OPROSCENO": "Oproščeno", "NI_PREDMET": "Ni predmet",
                 "OBRNJENA_DAVCNA": "Obrnjena davčna"}[mode]
    assert f"DDV: {treatment}" in html
    assert '<td class="amount">—</td>' in html
    assert '<td class="amount">22,00</td>' not in html
    assert context.vrstice[0].ddv_stopnja == Decimal("22.00")
    assert invoice.invoiceline_set.get().ddv_stopnja == Decimal("22.00")
    historical_html = pripravi_html(replace(context, pdf_template_version="1"))
    assert '<td class="amount">22,00</td>' in historical_html
    assert f"DDV: {treatment}" not in historical_html
    assert (context.osnova_skupaj, context.ddv_skupaj, context.skupaj_z_ddv) == (
        Decimal("0.03"), Decimal("0.00"), Decimal("0.03"),
    )
    assert render_pdf(invoice).pdf_bytes.startswith(b"%PDF-")


def test_unnumbered_st_is_rejected(ready):
    with pytest.raises(PdfRenderNapaka, match="številke"):
        render_pdf(draft(ready))


@pytest.mark.parametrize("changes", [
    {"status": "OSNUTEK"}, {"status": "ZA_PONOVNI_PREGLED"}, {"status": "invalid"},
    {"stevilka": "ST-2025-0001"}, {"stevilka": "ST-2026-0000"},
    {"stevilka": "NP-00001"}, {"stevilka": "ST-2026-1"},
    {"osnova_skupaj": None}, {"skupaj_z_ddv": Decimal("999.00")},
    {"rok_placila": None}, {"rok_placila": date(2020, 1, 1)},
    {"obdobje_do": date(2020, 1, 1)}, {"prejemnik_naziv": " "},
    {"prejemnik_identificiran_za_ddv": None}, {"izdajatelj_trr": ""},
    {"prejemnik_davcna_stevilka": "invalid"}, {"izdajatelj_trr": "invalid"},
    {"prejemnik_drzava_koda": "XX"},
    {"izdajatelj_logotip_slika_pot": "logo.png"},
])
def test_invalid_numbered_st_is_rejected_before_publication(confirmed, settings, changes):
    corrupt(confirmed, **changes)
    with pytest.raises(PdfRenderNapaka):
        render_pdf(confirmed)
    confirmed.refresh_from_db()
    assert not confirmed.rendered_pdf_path and not confirmed.checksum
    assert not list(settings.RENDERING_ROOT.glob("company-*/*.pdf"))


@pytest.mark.parametrize("changes", [
    {"enota_mere": ""}, {"ddv_nacin_obracuna": "INVALID"},
    {"ddv_rezim_sifra": ""}, {"ddv_zahteva_polni_odbitek": None},
    {"ddv_zahteva_polni_odbitek": True},
    {"ddv_znesek": Decimal("21.99")}, {"ddv_stopnja": Decimal("9.50")},
    {"referenca_listine": "listina", "datum_reference": None},
    {"referenca_listine": "", "datum_reference": date(2026, 1, 1)},
])
def test_invalid_st_line_is_rejected(confirmed, changes):
    models.QuerySet.update(confirmed.invoiceline_set.all(), **changes)
    with pytest.raises(PdfRenderNapaka):
        render_pdf(confirmed)


@pytest.mark.parametrize("failure", ["missing", "corrupt", "path", "checksum"])
def test_registered_st_artifact_fails_closed(confirmed, settings, monkeypatch, failure):
    result = render_pdf(confirmed)
    path = settings.RENDERING_ROOT / result.pot
    if failure == "missing":
        path.unlink()
    elif failure == "corrupt":
        path.write_bytes(b"changed")
    elif failure == "path":
        corrupt(confirmed, rendered_pdf_path="")
    else:
        corrupt(confirmed, checksum="")
    monkeypatch.setattr("rendering.services._generiraj_pdf_bytes",
                        lambda *a: pytest.fail("Registered PDF must not be regenerated"))
    with pytest.raises(PdfRenderNapaka) as error:
        render_pdf(confirmed)
    assert error.value.koda == "PDF_INTEGRITETA"
    assert stanje_koncnega_pdf(confirmed)["state"] == "ERROR"
    if failure == "missing":
        assert not path.exists()
    elif failure == "corrupt":
        assert path.read_bytes() == b"changed"


@pytest.mark.parametrize("failure", ["write", "metadata", "conflict"])
def test_st_publication_failure_never_registers_pdf(confirmed, settings, monkeypatch, failure):
    target = settings.RENDERING_ROOT / f"company-{confirmed.company_id}/ST-2026-0001.pdf"
    if failure == "conflict":
        target.parent.mkdir(parents=True)
        target.write_bytes(b"conflict")
    else:
        def fail(*args, **kwargs):
            raise OSError("test failure")
        monkeypatch.setattr("rendering.services." + (
            "os.link" if failure == "write" else "nastavi_pdf_artifact"), fail)
    with pytest.raises(PdfRenderNapaka):
        render_pdf(confirmed)
    confirmed.refresh_from_db()
    assert not confirmed.rendered_pdf_path and not confirmed.checksum
    if failure == "conflict":
        assert target.read_bytes() == b"conflict"
    else:
        assert not target.exists()


def test_master_data_edits_preserve_st_html_and_registered_bytes(confirmed, ready, monkeypatch):
    html = pripravi_html(pripravi_pdf_context(confirmed))
    result = render_pdf(confirmed)
    ready.company.naziv = "Spremenjen izdajatelj"
    save_financial_fixture(ready.company)
    ready.regime.stopnja = Decimal("9.50")
    ready.regime.zahteva_referenco = True
    save_financial_fixture(ready.regime)
    from tests.business_fixtures import save_business_record
    ready.lease.tenant.naziv = "Spremenjen prejemnik"
    save_business_record(ready.lease.tenant)
    signatory = ready.company.signatory_set.get()
    signatory.ime_priimek = "Drug podpisnik"
    save_business_record(signatory)
    assert pripravi_html(pripravi_pdf_context(confirmed)) == html
    monkeypatch.setattr("rendering.services.pripravi_pdf_context",
                        lambda *a: pytest.fail("Repeated render must reuse exact bytes"))
    assert render_pdf(confirmed) == result


def test_st_render_rejects_transaction_before_filesystem_access(confirmed, monkeypatch):
    for name in ("_preveri_obstojeci_artifact", "pripravi_pdf_context", "_zakleni_artifact"):
        monkeypatch.setattr(f"rendering.services.{name}",
                            lambda *a: pytest.fail("No file access inside transaction"))
    with transaction.atomic(), pytest.raises(PdfRenderNapaka) as error:
        render_pdf(confirmed)
    assert error.value.koda == "PDF_TRANSAKCIJA"


def test_matching_st_numbers_use_company_scoped_paths(confirmed, ready, settings):
    first = render_pdf(confirmed)
    other = Company.objects.exclude(pk=confirmed.company_id).first()
    other.aktivno = True
    save_financial_fixture(other)
    invoice = draft(ready, company=other)
    invoice = potrdi_osnutek_st(invoice, pregled=review(invoice))
    assert invoice.stevilka == confirmed.stevilka
    second = render_pdf(invoice)
    assert first.pot != second.pot
    assert (settings.RENDERING_ROOT / first.pot).read_bytes() == first.pdf_bytes
    assert (settings.RENDERING_ROOT / second.pot).read_bytes() == second.pdf_bytes


def test_historical_version_one_keeps_original_output(confirmed):
    corrupt(confirmed, pdf_template_version="1")
    models.QuerySet.delete(AuditLogEntry.objects.filter(object_id=confirmed.pk, polje="st_pdf_references_v1"))
    html = pripravi_html(pripravi_pdf_context(confirmed))
    assert "ST-2026-0001" in html
    assert "Enota mere:" not in html
    assert render_pdf(confirmed).pdf_bytes.startswith(b"%PDF-")


def test_st_cent_rounding_uses_persisted_values(ready):
    invoice = draft(ready, bruto=Decimal("0.03"))
    invoice = potrdi_osnutek_st(invoice, pregled=review(invoice))
    context = pripravi_pdf_context(invoice)
    assert (context.osnova_skupaj, context.ddv_skupaj, context.skupaj_z_ddv) == (
        Decimal("0.02"), Decimal("0.01"), Decimal("0.03"),
    )
    assert render_pdf(invoice).pdf_bytes.startswith(b"%PDF-")


@pytest.mark.parametrize("missing", [True, False])
def test_st_snapshot_asset_integrity_is_checked(confirmed, settings, missing):
    path = settings.MEDIA_ROOT / confirmed.izdajatelj_podpisnik_slika_pot
    if missing:
        path.unlink()
    else:
        path.write_bytes(b"changed")
    with pytest.raises(PdfRenderNapaka) as error:
        render_pdf(confirmed)
    assert error.value.koda in {"ASSET_MANJKA", "ASSET_NEVELJAVEN"}


def test_st_template_change_invalidates_individual_review(ready, settings):
    invoice = draft(ready)
    proof = review(invoice)
    settings.ST_PDF_TEMPLATE_VERSION = "changed"
    with pytest.raises(ValidationError, match="zastarel"):
        potrdi_osnutek_st(invoice, pregled=proof)


@pytest.mark.parametrize("required", [True, False])
def test_st_reference_requirement_comes_from_immutable_confirmation(ready, required, settings):
    ready.regime.zahteva_referenco = required
    save_financial_fixture(ready.regime)
    from companies.services import potrdi_ddv_nastavitve
    from tests.setup_helpers import approve_sample
    ready.item.referenca_listine = "LISTINA-1"
    ready.item.datum_reference = date(2026, 8, 15)
    save_financial_fixture(ready.item)
    potrdi_ddv_nastavitve(ready.company)
    approve_sample(ready.company, ready.lease)
    fields = ({"referenca_listine": "LISTINA-1", "datum_reference": date(2026, 8, 15)}
              if required else {})
    invoice = draft(ready, **fields)
    invoice = potrdi_osnutek_st(invoice, pregled=review(invoice))
    # Reverse the current catalog flag; only immutable confirmation evidence counts.
    ready.regime.zahteva_referenco = not required
    save_financial_fixture(ready.regime)
    models.QuerySet.update(invoice.invoiceline_set.all(), referenca_listine="", datum_reference=None)
    if required:
        with pytest.raises(PdfRenderNapaka) as error:
            render_pdf(invoice)
        assert error.value.koda == "ST_REFERENCA_NEVELJAVNA"
        invoice.refresh_from_db()
        assert not invoice.rendered_pdf_path and not invoice.checksum
        assert not list(settings.RENDERING_ROOT.glob("company-*/*.pdf"))
    else:
        assert render_pdf(invoice).pdf_bytes.startswith(b"%PDF-")


@pytest.mark.parametrize("fault", ["missing", "malformed", "duplicate", "line", "type", "number"])
def test_st_v2_reference_evidence_fails_closed(confirmed, fault):
    entry = AuditLogEntry.objects.get(object_id=confirmed.pk, polje="st_pdf_references_v1")
    if fault == "missing":
        models.QuerySet.delete(AuditLogEntry.objects.filter(pk=entry.pk))
    elif fault == "duplicate":
        entry.pk = None
        entry.save()
    else:
        evidence = json.loads(entry.nova_vrednost)
        if fault == "malformed":
            evidence = []
        elif fault == "line":
            evidence["lines"] = {}
        elif fault == "type":
            next(iter(evidence["lines"].values()))["required"] = "False"
        else:
            evidence["number"] = "ST-2026-9999"
        models.QuerySet.update(AuditLogEntry.objects.filter(pk=entry.pk), nova_vrednost=json.dumps(evidence))
    with pytest.raises(PdfRenderNapaka) as error:
        render_pdf(confirmed)
    assert error.value.koda == "ST_REFERENCA_NEVELJAVNA"


def test_registered_st_pdf_survives_missing_reference_evidence(confirmed):
    result = render_pdf(confirmed)
    models.QuerySet.delete(AuditLogEntry.objects.filter(object_id=confirmed.pk, polje="st_pdf_references_v1"))
    assert render_pdf(confirmed) == result


def test_reference_evidence_failure_rolls_back_confirmation(ready, monkeypatch):
    invoice = draft(ready)
    proof = review(invoice)
    before, numbers = Invoice.objects.filter(pk=invoice.pk).values().get(), counters()
    create = AuditLogEntry.objects.create
    def fail(*args, **kwargs):
        if kwargs.get("polje") == "st_pdf_references_v1":
            assert Invoice.objects.get(pk=invoice.pk).stevilka is not None
            raise RuntimeError("Evidence write failed")
        return create(*args, **kwargs)
    monkeypatch.setattr(AuditLogEntry.objects, "create", fail)
    with pytest.raises(RuntimeError, match="Evidence write failed"):
        potrdi_osnutek_st(invoice, pregled=proof)
    assert Invoice.objects.filter(pk=invoice.pk).values().get() == before
    assert counters() == numbers
