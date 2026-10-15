import base64
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.test import Client

from catalogs.models import VatRegime
from companies.models import (
    Company,
    CompanySampleConfirmation,
    NumberSequence,
    Signatory,
    VatSettingsConfirmation,
)
from companies.services import potrdi_ddv_nastavitve
from contracts.models import Lease, LeaseItem, Property, Tenant, Unit
from invoicing.models import Invoice, InvoiceLine, MonthlyBatch, MonthlyCycle
from tests.financial_fixtures import save_financial_fixture
from tests.setup_helpers import approve_sample

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg=="
)


@pytest.fixture
def confirmation_ui_data(db, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    settings.RENDERING_ROOT = tmp_path / "rendered"
    (tmp_path / "logo.png").write_bytes(PNG)
    (tmp_path / "podpis.png").write_bytes(PNG)
    company = Company.objects.create(
        naziv="Alfa d.o.o.", davcna_stevilka="15012557",
        maticna_stevilka="1234567000", naslov="Glavna 1", drzava_koda="SI",
        trr="SI56191000000123438", logotip="logo.png", ddv_zavezanec=True,
        email_racunovodja="rac@example.com", email_posiljatelj="racuni@example.com",
        keychain_kljuc_posiljatelja="alfa", ddv_razporeditev_potrjena=True,
    )
    Signatory.objects.create(
        company=company, ime_priimek="Ana Novak", podpis_slika="podpis.png",
        velja_od=date(2020, 1, 1),
    )
    property_ = Property.objects.create(company=company, naziv="Stavba A", naslov="A 1")
    unit = Unit.objects.create(
        property=property_, oznaka="P1", tip_enote=Unit.TipEnote.PISARNA
    )
    tenant = Tenant.objects.create(
        tip=Tenant.Tip.PRAVNA_OSEBA, naziv="Najemnik", davcna_stevilka="15012557",
        maticna_stevilka="1234567000", naslov="B 1", drzava_koda="SI",
        identificiran_za_ddv=True, email="n@example.com",
    )
    lease = Lease.objects.create(
        unit=unit, tenant=tenant, oznaka_pogodbe="P-1",
        tip_trajanja=Lease.TipTrajanja.NEDOLOCEN_CAS,
        datum_zacetka=date(2020, 1, 1), rok_placila_dni=15,
    )
    regime = VatRegime.objects.create(
        zahteva_polni_odbitek=False,
        sifra="R1", naziv="DDV 22", nacin_obracuna=VatRegime.NacinObracuna.OBDAVCENO,
        stopnja=Decimal("22.00"), rekapitulacijska_skupina="DDV 22 %",
        eslog_koda="S", velja_od=date(2020, 1, 1),
    )
    item = LeaseItem.objects.create(
        lease=lease, naziv_postavke="Najemnina",
        znesek_neto_mesecno=Decimal("100.00"), vat_regime=regime,
    )
    return SimpleNamespace(company=company, lease=lease, item=item, regime=regime)


def detail_url(data):
    return f"/nastavitve/podjetja/{data.company.pk}/"


def test_detail_shows_independent_states_review_and_ignores_legacy(
    client, confirmation_ui_data
):
    data = confirmation_ui_data
    content = client.get(detail_url(data)).content.decode()
    assert "DDV nastavitve" in content and "Vzorčni račun" in content
    assert content.count("Manjka") >= 2
    assert all(value in content for value in ("Stavba A", "P1", "P-1", "Najemnina", "R1"))

    potrdi_ddv_nastavitve(data.company)
    content = client.get(detail_url(data)).content.decode()
    assert "Veljavna" in content and "Manjka" in content


def test_vat_post_is_csrf_protected_and_preserves_history(client, confirmation_ui_data):
    data = confirmation_ui_data
    url = f"{detail_url(data)}potrdi-ddv/"
    assert client.get(url).status_code == 405
    assert Client(enforce_csrf_checks=True).post(url).status_code == 403

    before = (data.company.ddv_razporeditev_potrjena, MonthlyCycle.objects.count(), MonthlyBatch.objects.count())
    success = client.post(url, follow=True)
    assert success.status_code == 200
    assert "DDV nastavitve so potrjene." in success.content.decode()
    assert client.post(url).status_code == 302
    data.company.refresh_from_db()
    assert VatSettingsConfirmation.objects.filter(company=data.company).count() == 2
    assert (data.company.ddv_razporeditev_potrjena, MonthlyCycle.objects.count(), MonthlyBatch.objects.count()) == before


def test_review_excludes_unrelated_archived_and_unused_rows(client, confirmation_ui_data):
    data = confirmation_ui_data
    unused = VatRegime.objects.create(
        zahteva_polni_odbitek=False,
        sifra="NEUP", naziv="Neuporabljen", nacin_obracuna=VatRegime.NacinObracuna.OPROSCENO,
        stopnja=Decimal(0), rekapitulacijska_skupina="O", eslog_koda="E",
        velja_od=date(2020, 1, 1),
    )
    data.item.arhivirana = True
    save_financial_fixture(data.item, update_fields=["arhivirana"])
    content = client.get(detail_url(data)).content.decode()
    assert "Najemnina" not in content and unused.naziv not in content


def test_sample_choices_scope_preview_and_confirmation(
    client, confirmation_ui_data, monkeypatch
):
    data = confirmation_ui_data
    other = Company.objects.create(
        naziv="Druga", davcna_stevilka="25012550", maticna_stevilka="1234567001",
        naslov="C 1", drzava_koda="SI", trr="SI56191000000123438", logotip="logo.png",
        email_racunovodja="r2@example.com", email_posiljatelj="p2@example.com",
        keychain_kljuc_posiljatelja="druga",
    )
    other_property = Property.objects.create(company=other, naziv="Druga stavba", naslov="C 1")
    other_unit = Unit.objects.create(property=other_property, oznaka="D1", tip_enote=Unit.TipEnote.PISARNA)
    other_lease = Lease.objects.create(
        unit=other_unit, tenant=data.lease.tenant, oznaka_pogodbe="TUJA",
        tip_trajanja=Lease.TipTrajanja.NEDOLOCEN_CAS,
        datum_zacetka=date(2020, 1, 1), rok_placila_dni=15,
    )
    content = client.get(detail_url(data)).content.decode()
    assert "P-1" in content and "TUJA" not in content
    preview_start = '<form method="get" action="'
    selection_form = preview_start + content.split(preview_start, 1)[1].split(
        "</form>", 1
    )[0]
    assert 'class="sample-selection-form"' in selection_form
    assert "csrfmiddlewaretoken" not in selection_form
    assert client.post(f"{detail_url(data)}potrdi-vzorec/", {"lease": other_lease.pk}).status_code == 404

    counts = (Invoice.objects.count(), InvoiceLine.objects.count(), NumberSequence.objects.count())
    potrdi_ddv_nastavitve(data.company)
    selected = client.get(
        detail_url(data), {"vzorec_pogodba": data.lease.pk}
    ).content.decode()
    preview_url = f"{detail_url(data)}vzorec/{data.lease.pk}/pdf/"
    assert f'href="{preview_url}"' in selected
    assert "csrfmiddlewaretoken" not in selection_form
    preview = client.get(preview_url)
    assert preview.status_code == 200
    assert preview["Content-Type"] == "application/pdf"
    assert preview["Content-Disposition"].startswith("inline")
    assert preview["Cache-Control"] == "no-store"
    assert (Invoice.objects.count(), InvoiceLine.objects.count(), NumberSequence.objects.count()) == counts


def test_one_selected_lease_drives_preview_and_confirmation(
    client, confirmation_ui_data
):
    data = confirmation_ui_data
    unit_b = Unit.objects.create(
        property=data.lease.unit.property,
        oznaka="P2",
        tip_enote=Unit.TipEnote.PISARNA,
    )
    lease_b = Lease.objects.create(
        unit=unit_b,
        tenant=data.lease.tenant,
        oznaka_pogodbe="P-2",
        tip_trajanja=Lease.TipTrajanja.NEDOLOCEN_CAS,
        datum_zacetka=date(2020, 1, 1),
        rok_placila_dni=15,
    )
    LeaseItem.objects.create(
        lease=lease_b,
        naziv_postavke="Najemnina B",
        znesek_neto_mesecno=Decimal("200.00"),
        vat_regime=data.regime,
    )
    potrdi_ddv_nastavitve(data.company)

    response = client.get(detail_url(data), {"vzorec_pogodba": lease_b.pk})
    content = response.content.decode()
    preview_url = f"{detail_url(data)}vzorec/{lease_b.pk}/pdf/"

    assert response.status_code == 200
    assert "Pogodba za vzorec:</strong> Stavba A — P2 — P-2 — Najemnik" in content
    assert f'href="{preview_url}"' in content
    assert f'<input type="hidden" name="lease" value="{lease_b.pk}">' in content
    assert f'<input type="hidden" name="lease" value="{data.lease.pk}">' not in content
    assert content.count('id="id_vzorec_pogodba"') == 1
    assert 'id="id_lease"' not in content

    assert client.get(preview_url).status_code == 200
    confirmation = client.post(
        f"{detail_url(data)}potrdi-vzorec/", {"lease": lease_b.pk}
    )
    assert confirmation.status_code == 302
    assert CompanySampleConfirmation.objects.get().lease_id == lease_b.pk


@pytest.mark.parametrize("marker", ["ddv", "vzorec"])
def test_known_success_marker_without_valid_confirmation_shows_no_message(
    client, confirmation_ui_data, marker
):
    content = client.get(
        f"{detail_url(confirmation_ui_data)}?potrditev={marker}"
    ).content.decode()
    assert "DDV nastavitve so potrjene." not in content
    assert "Vzorčni račun je potrjen." not in content


@pytest.mark.parametrize("suffix", ["?potrditev=fake", ""])
def test_unknown_or_missing_success_marker_shows_no_message(
    client, confirmation_ui_data, suffix
):
    content = client.get(f"{detail_url(confirmation_ui_data)}{suffix}").content.decode()
    assert "DDV nastavitve so potrjene." not in content
    assert "Vzorčni račun je potrjen." not in content


def test_sample_post_is_immutable_safe_and_errors_are_controlled(client, confirmation_ui_data):
    data = confirmation_ui_data
    potrdi_ddv_nastavitve(data.company)
    url = f"{detail_url(data)}potrdi-vzorec/"
    assert client.get(url).status_code == 405
    counts = (
        Invoice.objects.count(), InvoiceLine.objects.count(),
        NumberSequence.objects.count(), MonthlyCycle.objects.count(),
        MonthlyBatch.objects.count(),
    )
    assert client.post(url, {"lease": data.lease.pk}).status_code == 409
    preview_url = f"{detail_url(data)}vzorec/{data.lease.pk}/pdf/"
    assert client.get(preview_url).status_code == 200
    success = client.post(url, {"lease": data.lease.pk}, follow=True)
    assert success.status_code == 200
    assert "Vzorčni račun je potrjen." in success.content.decode()
    assert client.post(url, {"lease": data.lease.pk}).status_code == 302
    assert CompanySampleConfirmation.objects.filter(company=data.company).count() == 2
    assert (
        Invoice.objects.count(), InvoiceLine.objects.count(),
        NumberSequence.objects.count(), MonthlyCycle.objects.count(),
        MonthlyBatch.objects.count(),
    ) == counts

    Signatory.objects.filter(company=data.company).update(velja_do=date(2020, 1, 2))
    response = client.post(url, {"lease": data.lease.pk})
    assert response.status_code == 409 and "Ni veljavnega podpisnika" in response.content.decode()


def test_stale_states_and_dashboard_tasks(client, confirmation_ui_data):
    data = confirmation_ui_data
    potrdi_ddv_nastavitve(data.company)
    approve_sample(data.company, data.lease)
    assert "Potrebne potrditve" not in client.get("/").content.decode()

    data.regime.stopnja = Decimal("9.50")
    save_financial_fixture(data.regime, update_fields=["stopnja"])
    content = client.get(detail_url(data)).content.decode()
    dashboard = client.get("/").content.decode()
    assert content.count("Zastarela") >= 2
    assert "Potrebne potrditve" in dashboard
    assert detail_url(data) in dashboard
