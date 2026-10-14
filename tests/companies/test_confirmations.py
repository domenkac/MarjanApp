import base64
from datetime import date
from decimal import Decimal
from hashlib import sha256

import pytest
from django.core.files.base import ContentFile

from catalogs.models import VatRegime
from companies import services as company_services
from companies.models import (
    Company,
    CompanySampleConfirmation,
    NumberSequence,
    Signatory,
    VatSettingsConfirmation,
)
from companies.services import (
    FINGERPRINT_DDV_VERSION,
    FINGERPRINT_VZORCA_VERSION,
    AssetPotrditveSpremenjen,
    odloci_o_fizicnem_izbrisu,
    potrdi_ddv_nastavitve,
    veljavna_potrditev_ddv,
    veljavna_potrditev_vzorca,
)
from contracts.models import Lease, LeaseItem, Property, Tenant, Unit
from contracts.services import odloci_o_fizicnem_izbrisu as odloci_o_izbrisu_pogodbe
from invoicing.models import Invoice, InvoiceLine
from tests.setup_helpers import approve_sample as _approve_sample

PNG_1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg=="
)


def approve_sample(company, lease):
    potrdi_ddv_nastavitve(company)
    return _approve_sample(company, lease)


PNG_2 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYPj/HwADAgH/5ncLrgAAAABJRU5ErkJggg=="
)


@pytest.fixture
def confirmation_data(db, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    (tmp_path / "logo.png").write_bytes(PNG_1)
    (tmp_path / "podpis.png").write_bytes(PNG_2)
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
        podpis_slika="podpis.png",
        velja_od=date(2026, 1, 1),
    )
    property_ = Property.objects.create(
        company=company,
        naziv="Stavba A",
        naslov="Glavna 1",
    )
    unit = Unit.objects.create(
        property=property_,
        oznaka="Pisarna 1",
        tip_enote=Unit.TipEnote.PISARNA,
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
        oznaka_pogodbe="12/2024",
        tip_trajanja=Lease.TipTrajanja.NEDOLOCEN_CAS,
        datum_zacetka=date(2026, 1, 1),
        rok_placila_dni=15,
    )
    used_regime = VatRegime.objects.create(
        zahteva_polni_odbitek=False,
        sifra="R1",
        naziv="DDV 22",
        nacin_obracuna=VatRegime.NacinObracuna.OBDAVCENO,
        stopnja=Decimal("22.00"),
        rekapitulacijska_skupina="DDV 22 %",
        eslog_koda="S",
        velja_od=date(2026, 1, 1),
    )
    unused_regime = VatRegime.objects.create(
        zahteva_polni_odbitek=False,
        sifra="R2",
        naziv="DDV 9,5",
        nacin_obracuna=VatRegime.NacinObracuna.OBDAVCENO,
        stopnja=Decimal("9.50"),
        rekapitulacijska_skupina="DDV 9,5 %",
        eslog_koda="S",
        velja_od=date(2026, 1, 1),
    )
    item = LeaseItem.objects.create(
        lease=lease,
        naziv_postavke="Najemnina",
        znesek_neto_mesecno=Decimal("100.00"),
        vat_regime=used_regime,
        referenca_listine="P-1",
        datum_reference=date(2026, 1, 1),
    )
    return {
        "company": company,
        "signatory": signatory,
        "tenant": tenant,
        "lease": lease,
        "item": item,
        "used_regime": used_regime,
        "unused_regime": unused_regime,
    }


def test_ddv_confirmation_lifecycle_and_legacy_boolean(confirmation_data):
    company = confirmation_data["company"]
    assert veljavna_potrditev_ddv(company).stanje == "missing"

    confirmation = potrdi_ddv_nastavitve(company)

    assert confirmation.confirmed_at is not None
    assert len(confirmation.source_fingerprint) == 64
    assert confirmation.fingerprint_schema_version == FINGERPRINT_DDV_VERSION
    assert veljavna_potrditev_ddv(company).stanje == "valid"


@pytest.mark.parametrize(
    ("target", "field", "value"),
    [
        ("company", "ddv_zavezanec", False),
        ("used_regime", "stopnja", Decimal("20.00")),
    ],
)
def test_ddv_semantic_change_is_stale(confirmation_data, target, field, value):
    company = confirmation_data["company"]
    potrdi_ddv_nastavitve(company)

    type(confirmation_data[target]).objects.filter(
        pk=confirmation_data[target].pk
    ).update(**{field: value})

    assert veljavna_potrditev_ddv(company).stanje == "stale"


def test_ddv_allocation_change_is_stale(confirmation_data):
    company = confirmation_data["company"]
    potrdi_ddv_nastavitve(company)

    LeaseItem.objects.filter(pk=confirmation_data["item"].pk).update(
        vat_regime=confirmation_data["unused_regime"]
    )

    assert veljavna_potrditev_ddv(company).stanje == "stale"


def test_unused_regime_change_does_not_stale_ddv(confirmation_data):
    company = confirmation_data["company"]
    potrdi_ddv_nastavitve(company)

    VatRegime.objects.filter(pk=confirmation_data["unused_regime"].pk).update(
        stopnja=Decimal("8.50")
    )

    assert veljavna_potrditev_ddv(company).stanje == "valid"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("znesek_neto_mesecno", Decimal("120.00")),
        ("naziv_postavke", "Druga najemnina"),
        ("referenca_listine", "P-2"),
        ("datum_reference", date(2026, 2, 1)),
    ],
)
def test_non_vat_item_change_does_not_stale_ddv(confirmation_data, field, value):
    company = confirmation_data["company"]
    potrdi_ddv_nastavitve(company)

    LeaseItem.objects.filter(pk=confirmation_data["item"].pk).update(**{field: value})

    assert veljavna_potrditev_ddv(company).stanje == "valid"


def test_prior_ddv_state_becomes_valid_after_exact_restoration(confirmation_data):
    company = confirmation_data["company"]
    original = potrdi_ddv_nastavitve(company)
    Company.objects.filter(pk=company.pk).update(ddv_zavezanec=False)
    potrdi_ddv_nastavitve(company)
    Company.objects.filter(pk=company.pk).update(ddv_zavezanec=True)

    result = veljavna_potrditev_ddv(company)

    assert result.stanje == "valid"
    assert result.potrditev == original


def test_sample_confirmation_lifecycle_and_safety(confirmation_data, settings):
    company = confirmation_data["company"]
    lease = confirmation_data["lease"]
    before = (
        Invoice.objects.count(),
        InvoiceLine.objects.count(),
        NumberSequence.objects.count(),
    )

    confirmation = approve_sample(company, lease)

    assert confirmation.company == company
    assert confirmation.lease == lease
    assert confirmation.confirmed_at is not None
    assert len(confirmation.source_fingerprint) == 64
    assert confirmation.pdf_template_version == settings.PDF_TEMPLATE_VERSION
    assert confirmation.fingerprint_schema_version == FINGERPRINT_VZORCA_VERSION
    assert veljavna_potrditev_vzorca(company).stanje == "valid"
    assert (
        Invoice.objects.count(),
        InvoiceLine.objects.count(),
        NumberSequence.objects.count(),
    ) == before

    company.refresh_from_db()
    confirmation_data["signatory"].refresh_from_db()
    assert company.logotip_checksum == sha256(PNG_1).hexdigest()
    assert confirmation_data["signatory"].podpis_checksum == sha256(PNG_2).hexdigest()


@pytest.mark.parametrize(
    ("target", "field", "value"),
    [
        ("item", "znesek_neto_mesecno", Decimal("120.00")),
        ("item", "referenca_listine", "P-2"),
        ("company", "naziv", "Beta d.o.o."),
        ("company", "trr", "SI56263300012039086"),
        ("tenant", "naziv", "Drug najemnik d.o.o."),
        ("used_regime", "stopnja", Decimal("20.00")),
    ],
)
def test_sample_semantic_change_is_stale(confirmation_data, target, field, value):
    company = confirmation_data["company"]
    approve_sample(company, confirmation_data["lease"])

    type(confirmation_data[target]).objects.filter(
        pk=confirmation_data[target].pk
    ).update(**{field: value})

    assert veljavna_potrditev_vzorca(company).stanje == "stale"


def test_unrelated_tenant_change_does_not_stale_sample(confirmation_data):
    company = confirmation_data["company"]
    approve_sample(company, confirmation_data["lease"])
    Tenant.objects.create(
        tip=Tenant.Tip.FIZICNA_OSEBA,
        naziv="Nepovezan najemnik",
        naslov="Druga 2",
        drzava_koda="SI",
        identificiran_za_ddv=False,
        email="drug@example.com",
    )

    assert veljavna_potrditev_vzorca(company).stanje == "valid"


def test_logo_filefield_replacement_stales_sample(confirmation_data):
    company = confirmation_data["company"]
    approve_sample(company, confirmation_data["lease"])
    staro_ime = company.logotip.name

    company.logotip.save("nov-logo.png", ContentFile(PNG_2), save=True)

    assert company.logotip.name != staro_ime
    assert veljavna_potrditev_vzorca(company).stanje == "stale"


def test_signature_filefield_replacement_stales_sample(confirmation_data):
    company = confirmation_data["company"]
    signatory = confirmation_data["signatory"]
    approve_sample(company, confirmation_data["lease"])
    staro_ime = signatory.podpis_slika.name

    signatory.podpis_slika.save("nov-podpis.png", ContentFile(PNG_1), save=True)

    assert signatory.podpis_slika.name != staro_ime
    assert veljavna_potrditev_vzorca(company).stanje == "stale"


def test_unrelated_lease_amount_does_not_stale_sample(confirmation_data):
    company = confirmation_data["company"]
    selected_lease = confirmation_data["lease"]
    other_tenant = Tenant.objects.create(
        tip=Tenant.Tip.FIZICNA_OSEBA,
        naziv="Drugi najemnik",
        naslov="Druga 2",
        drzava_koda="SI",
        identificiran_za_ddv=False,
        email="drug@example.com",
    )
    other_lease = Lease.objects.create(
        unit=selected_lease.unit,
        tenant=other_tenant,
        oznaka_pogodbe="13/2024",
        tip_trajanja=Lease.TipTrajanja.NEDOLOCEN_CAS,
        datum_zacetka=date(2026, 1, 1),
        rok_placila_dni=15,
    )
    other_item = LeaseItem.objects.create(
        lease=other_lease,
        naziv_postavke="Druga najemnina",
        znesek_neto_mesecno=Decimal("50.00"),
        vat_regime=confirmation_data["used_regime"],
    )
    approve_sample(company, selected_lease)

    LeaseItem.objects.filter(pk=other_item.pk).update(
        znesek_neto_mesecno=Decimal("60.00")
    )

    assert veljavna_potrditev_vzorca(company).stanje == "valid"


def test_sample_validity_does_not_read_storage(confirmation_data, monkeypatch):
    company = confirmation_data["company"]
    approve_sample(company, confirmation_data["lease"])

    def nepricakovano_branje(*_args, **_kwargs):
        raise AssertionError("Validity ne sme brati shrambe.")

    monkeypatch.setattr(company_services.default_storage, "open", nepricakovano_branje)

    assert veljavna_potrditev_vzorca(company).stanje == "valid"


def test_asset_change_during_confirmation_requires_retry(
    confirmation_data, monkeypatch
):
    company = confirmation_data["company"]
    original = company_services._checksum_immutable_asseta
    calls = 0

    def checksum_with_change(path):
        nonlocal calls
        checksum = original(path)
        calls += 1
        if calls == 1:
            Company.objects.filter(pk=company.pk).update(logotip="drug-logo.png")
        return checksum

    monkeypatch.setattr(
        company_services, "_checksum_immutable_asseta", checksum_with_change
    )

    with pytest.raises(AssetPotrditveSpremenjen):
        approve_sample(company, confirmation_data["lease"])
    assert not CompanySampleConfirmation.objects.filter(company=company).exists()


def test_pdf_version_change_stales_sample(confirmation_data, settings):
    company = confirmation_data["company"]
    approve_sample(company, confirmation_data["lease"])

    settings.PDF_TEMPLATE_VERSION = "2"

    assert veljavna_potrditev_vzorca(company).stanje == "stale"


def test_prior_sample_state_becomes_valid_after_exact_restoration(confirmation_data):
    company = confirmation_data["company"]
    item = confirmation_data["item"]
    original = approve_sample(company, confirmation_data["lease"])
    LeaseItem.objects.filter(pk=item.pk).update(znesek_neto_mesecno=Decimal("120.00"))
    approve_sample(company, confirmation_data["lease"])
    LeaseItem.objects.filter(pk=item.pk).update(znesek_neto_mesecno=Decimal("100.00"))

    result = veljavna_potrditev_vzorca(company)

    assert result.stanje == "valid"
    assert result.potrditev == original


def test_confirmation_history_protects_company_and_lease(confirmation_data):
    company = confirmation_data["company"]
    lease = confirmation_data["lease"]
    potrdi_ddv_nastavitve(company)
    approve_sample(company, lease)
    Company.objects.filter(pk=company.pk).update(ddv_razporeditev_potrjena=False)

    company_result = odloci_o_fizicnem_izbrisu(company, izrecna_potrditev=True)
    lease_result = odloci_o_izbrisu_pogodbe(lease, izrecna_potrditev=True)

    assert VatSettingsConfirmation.objects.filter(company=company).exists()
    assert CompanySampleConfirmation.objects.filter(
        company=company, lease=lease
    ).exists()
    assert any(
        reason.koda == "POVEZANA_POTRDITEV"
        and reason.tip_povezave == "VatSettingsConfirmation"
        for reason in company_result.razlogi
    )
    assert any(
        reason.koda == "POVEZANA_POTRDITEV"
        and reason.tip_povezave == "CompanySampleConfirmation"
        for reason in lease_result.razlogi
    )
