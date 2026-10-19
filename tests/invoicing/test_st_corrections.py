from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.db import connection

from catalogs.models import VatRegime
from companies.models import NumberSequence
from companies.services import potrdi_ddv_nastavitve
from invoicing.models import Invoice
from invoicing.services import (
    dodaj_vrstico,
    fingerprint_osnutka_st,
    potrdi_korekcijski_osnutek,
    potrdi_osnutek_st,
    preglej_osnutek_st,
    pripravi_korekcijski_osnutek,
    pripravi_osnutek_st,
    spremeni_vrstico,
)
from rendering.services import render_pdf
from tests.business_fixtures import save_business_record
from tests.financial_fixtures import save_financial_fixture
from tests.invoicing.test_korekcije import izdan_np
from tests.invoicing.test_priprava_np import pogodba as pogodba  # noqa: PLC0414
from tests.setup_helpers import approve_sample

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def standalone(pogodba, settings, tmp_path):
    settings.RENDERING_ROOT = tmp_path / "rendered"
    np = izdan_np(pogodba)
    st = pripravi_osnutek_st(
        company=np.company, prejemnik=np.lease.tenant,
        vat_regime=np.lease.leaseitem_set.first().vat_regime,
        datum_izdaje=date(2026, 9, 1), obdobje_od=date(2026, 8, 1), obdobje_do=date(2026, 8, 31),
        bruto=Decimal("122.00"), opis="Zgodovinska storitev", enota_mere="ura",
        referenca_listine="IZVIRNA-1", datum_reference=date(2026, 8, 1),
    )
    review = preglej_osnutek_st(st, expected_fingerprint=fingerprint_osnutka_st(st), individual_review=True)
    return potrdi_osnutek_st(st, pregled=review)


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch):
    for target in ("socket.create_connection", "smtplib.SMTP", "smtplib.SMTP_SSL", "keyring.get_password"):
        monkeypatch.setattr(target, lambda *args, **kwargs: pytest.fail("External service call"))


def credit_data(source):
    return {name: getattr(source, name) for name in (
        "opis", "enota_mere", "ddv_stopnja", "ddv_nacin_obracuna", "ddv_eslog_koda",
        "ddv_rekap_skupina", "ddv_klavzula", "referenca_listine", "datum_reference",
    )} | {"osnova": Decimal("-10.00"), "ddv_znesek": Decimal("-2.20"), "skupaj": Decimal("-12.20")}


@pytest.mark.parametrize("kind", ["STORNO", "DOBROPIS"])
def test_genuine_st_correction_preserves_history_and_renders(standalone, kind):
    original = standalone
    before = Invoice.objects.values().get(pk=original.pk)
    lines = list(original.invoiceline_set.order_by("pk").values())
    correction = pripravi_korekcijski_osnutek(original, kind, "Napaka zneska", date(2027, 1, 2))
    source = original.invoiceline_set.get()
    if kind == "DOBROPIS":
        dodaj_vrstico(correction, izvirna_vrstica=source, **credit_data(source))
    # Changing the live catalog after issuance must not rewrite or invalidate history.
    regime = VatRegime.objects.get(sifra=source.ddv_rezim_sifra)
    regime.stopnja = Decimal("9.50")
    save_financial_fixture(regime, update_fields=["stopnja"])
    potrdi_ddv_nastavitve(original.company)
    approve_sample(original.company, Invoice.objects.get(document_type="NP").lease)
    issued = potrdi_korekcijski_osnutek(correction)
    assert issued.stevilka == "ST-2027-0001"
    assert issued.lease_id is None and issued.eslog_konfiguracija_snapshot is None
    result = render_pdf(issued)
    assert result.pdf_bytes.startswith(b"%PDF-")
    assert Invoice.objects.values().get(pk=original.pk) == before
    assert list(original.invoiceline_set.order_by("pk").values()) == lines
    with pytest.raises((ValidationError, ValueError)):
        potrdi_korekcijski_osnutek(issued)
    assert NumberSequence.objects.get(company=original.company, tip="ST", leto=2027).zadnja_stevilka == 1


def test_credit_source_metadata_survives_edit_and_catalog_change(standalone):
    correction = pripravi_korekcijski_osnutek(standalone, "DOBROPIS", "Napaka", date(2027, 1, 2))
    source = standalone.invoiceline_set.get()
    line = dodaj_vrstico(correction, izvirna_vrstica=source, **credit_data(source))
    assert (line.ddv_rezim_sifra, line.ddv_zahteva_polni_odbitek) == (
        source.ddv_rezim_sifra, source.ddv_zahteva_polni_odbitek,
    )
    spremeni_vrstico(line, opis="Urejen opis", osnova=Decimal("-20.00"),
                     ddv_znesek=Decimal("-4.40"), skupaj=Decimal("-24.40"))
    line.refresh_from_db()
    assert line.ddv_rezim_sifra == source.ddv_rezim_sifra
    assert line.ddv_zahteva_polni_odbitek is source.ddv_zahteva_polni_odbitek
    for changes in ({"ddv_rezim_sifra": "FORGED"}, {"ddv_zahteva_polni_odbitek": None},
                    {"ddv_stopnja": Decimal("9.50")}, {"ddv_klavzula": "Druga klavzula"}):
        with pytest.raises((ValidationError, ValueError)):
            spremeni_vrstico(line, **changes)


def test_foreign_source_and_raw_metadata_rejected(standalone):
    correction = pripravi_korekcijski_osnutek(standalone, "DOBROPIS", "Napaka", date(2027, 1, 2))
    source = standalone.invoiceline_set.get()
    foreign = Invoice.objects.filter(document_type="NP").first().invoiceline_set.first()
    with pytest.raises((ValidationError, ValueError)):
        dodaj_vrstico(correction, izvirna_vrstica=foreign, **credit_data(source))
    with pytest.raises((ValidationError, ValueError)):
        dodaj_vrstico(correction, ddv_rezim_sifra="FORGED", **credit_data(source))
    with pytest.raises((ValidationError, ValueError)):
        dodaj_vrstico(correction, **credit_data(source))
    assert not correction.invoiceline_set.exists()


@pytest.mark.parametrize("gate", ["feature", "inactive", "missing_reference_evidence"])
def test_st_correction_fails_closed(standalone, settings, gate):
    correction = pripravi_korekcijski_osnutek(standalone, "STORNO", "Napaka", date(2027, 1, 2))
    if gate == "feature":
        settings.FEATURE_ESLOG = True
    elif gate == "inactive":
        company = standalone.company
        company.aktivno = False
        save_financial_fixture(company, update_fields=["aktivno"])
    target = "invoicing.st_confirmation.preveri_st_pdf_referencne_zahteve"
    if gate == "missing_reference_evidence":
        with (patch(target, side_effect=ValidationError("Missing historical evidence")),
              pytest.raises((ValidationError, ValueError))):
            potrdi_korekcijski_osnutek(correction)
    else:
        with pytest.raises((ValidationError, ValueError)):
            potrdi_korekcijski_osnutek(correction)
    correction.refresh_from_db()
    assert correction.stevilka is None
    assert not NumberSequence.objects.filter(company=standalone.company, tip="ST", leto=2027).exists()


def test_st_numbering_has_no_filesystem_operations_in_transaction(standalone):
    correction = pripravi_korekcijski_osnutek(standalone, "STORNO", "Napaka", date(2027, 1, 2))
    from invoicing.st_confirmation import _assets

    def assets(*args):
        assert not connection.in_atomic_block
        return _assets(*args)

    with patch("invoicing.st_confirmation._assets", side_effect=assets):
        assert potrdi_korekcijski_osnutek(correction).stevilka == "ST-2027-0001"


@pytest.mark.parametrize("kind", ["STORNO", "DOBROPIS"])
def test_reference_requirements_and_full_deduction_are_historical(standalone, kind):
    tenant = standalone.prejemnik
    tenant.ddv_zavezanec_s_polnim_odbitkom = True
    save_business_record(tenant, update_fields=["ddv_zavezanec_s_polnim_odbitkom"])
    regime = VatRegime.objects.create(
        sifra="ST_ZGODOVINSKI", naziv="Posebna storitev", nacin_obracuna="OBDAVCENO",
        stopnja=Decimal("22.00"), rekapitulacijska_skupina="OBDAVCENO", eslog_koda="S",
        zahteva_polni_odbitek=True, zahteva_referenco=True, velja_od=date(2026, 1, 1),
    )
    lease = Invoice.objects.get(document_type="NP").lease
    potrdi_ddv_nastavitve(standalone.company)
    approve_sample(standalone.company, lease)
    st = pripravi_osnutek_st(
        company=standalone.company, prejemnik=tenant, vat_regime=regime,
        datum_izdaje=date(2026, 9, 2), obdobje_od=date(2026, 8, 1), obdobje_do=date(2026, 8, 31),
        bruto=Decimal("122.00"), opis="Storitev z referenco", enota_mere="ura",
        referenca_listine="LISTINA-9", datum_reference=date(2026, 8, 1),
    )
    review = preglej_osnutek_st(st, expected_fingerprint=fingerprint_osnutka_st(st), individual_review=True)
    st = potrdi_osnutek_st(st, pregled=review)
    correction = pripravi_korekcijski_osnutek(st, kind, "Napaka", date(2027, 1, 2))
    source = st.invoiceline_set.get()
    if kind == "DOBROPIS":
        with pytest.raises(ValidationError, match="referenca"):
            dodaj_vrstico(correction, izvirna_vrstica=source,
                         **(credit_data(source) | {"referenca_listine": "", "datum_reference": None}))
        line = dodaj_vrstico(correction, izvirna_vrstica=source, **credit_data(source))
        with pytest.raises(ValidationError, match="referenca"):
            spremeni_vrstico(line, referenca_listine="", datum_reference=None)
    else:
        line = correction.invoiceline_set.get()
    assert line.ddv_rezim_sifra == "ST_ZGODOVINSKI" and line.ddv_zahteva_polni_odbitek is True
    assert (line.enota_mere, line.referenca_listine, line.datum_reference) == ("ura", "LISTINA-9", date(2026, 8, 1))
    # These mutable catalog changes cannot remove the issued reference requirement.
    regime.zahteva_referenco = False
    regime.zahteva_polni_odbitek = False
    save_financial_fixture(regime, update_fields=["zahteva_referenco", "zahteva_polni_odbitek"])
    potrdi_ddv_nastavitve(standalone.company)
    approve_sample(standalone.company, lease)
    issued = potrdi_korekcijski_osnutek(correction)
    assert render_pdf(issued).pdf_bytes.startswith(b"%PDF-")
    line.refresh_from_db()
    assert line.ddv_zahteva_polni_odbitek is True


@pytest.mark.parametrize("field,value", [("prejemnik_naziv", "Drug prejemnik"),
                                         ("objekt_naziv", "Drug objekt")])
def test_substituted_correction_snapshots_rejected(standalone, field, value):
    correction = pripravi_korekcijski_osnutek(standalone, "STORNO", "Napaka", date(2027, 1, 2))
    Invoice.objects.filter(pk=correction.pk).update(**{field: value})
    with pytest.raises((ValidationError, ValueError)):
        potrdi_korekcijski_osnutek(correction)
    assert not NumberSequence.objects.filter(company=standalone.company, tip="ST", leto=2027).exists()


def test_corrections_and_drafts_are_invalid_st_sources(standalone):
    for source in (None, Invoice(document_type="ST")):
        with pytest.raises(ValueError):
            pripravi_korekcijski_osnutek(source, "STORNO", "Napaka", date(2027, 1, 2))
    draft = pripravi_korekcijski_osnutek(standalone, "STORNO", "Napaka", date(2027, 1, 2))
    with pytest.raises(ValueError):
        pripravi_korekcijski_osnutek(draft, "DOBROPIS", "Napaka", date(2027, 1, 3))
    issued = potrdi_korekcijski_osnutek(draft)
    unissued = pripravi_osnutek_st(
        company=standalone.company, prejemnik=standalone.prejemnik,
        vat_regime=VatRegime.objects.get(sifra=standalone.invoiceline_set.get().ddv_rezim_sifra),
        datum_izdaje=date(2026, 9, 3), obdobje_od=date(2026, 8, 1), obdobje_do=date(2026, 8, 31),
        bruto=Decimal("122.00"), opis="Neizdana storitev", enota_mere="ura",
    )
    for source in (unissued, issued):
        with pytest.raises(ValueError):
            pripravi_korekcijski_osnutek(source, "DOBROPIS", "Napaka", date(2027, 1, 3))


def test_st_without_immutable_issuance_evidence_is_not_a_source(standalone):
    values = Invoice.objects.values().get(pk=standalone.pk)
    values.pop("id")
    values["stevilka"] = "ST-2026-0099"
    unverified = Invoice.objects.create(**values)
    before = Invoice.objects.count()
    with pytest.raises(ValidationError, match="dokazilo"):
        pripravi_korekcijski_osnutek(unverified, "STORNO", "Napaka", date(2027, 1, 2))
    assert Invoice.objects.count() == before


def test_st_confirmation_failure_rolls_back_number_and_reference_evidence(standalone):
    correction = pripravi_korekcijski_osnutek(standalone, "STORNO", "Napaka", date(2027, 1, 2))
    with (patch("invoicing.corrections.record_st_correction_pdf", side_effect=RuntimeError("Evidence failure")),
          pytest.raises(RuntimeError)):
        potrdi_korekcijski_osnutek(correction)
    correction.refresh_from_db()
    assert correction.stevilka is None
    assert not NumberSequence.objects.filter(company=standalone.company, tip="ST", leto=2027).exists()
