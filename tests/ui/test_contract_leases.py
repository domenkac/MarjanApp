from datetime import date
from decimal import Decimal

import pytest
from django.contrib.contenttypes.models import ContentType
from django.test import Client
from django.urls import NoReverseMatch, reverse

from catalogs.models import VatRegime
from companies.models import Company
from contracts.models import Lease, LeaseItem, Property, Tenant, Unit
from contracts.renewals import lease_source_fingerprint
from core.models import AuditLogEntry
from core.services import record_edit_fingerprint
from invoicing.models import MonthlyBatch
from invoicing.services import shrani_prvi_korak
from tests.business_fixtures import save_business_record
from tests.financial_fixtures import save_financial_fixture


def make_company(**changes):
    data = {
        "naziv": "Alfa d.o.o.", "davcna_stevilka": "15012557",
        "maticna_stevilka": "1234567000", "naslov": "Glavna 1",
        "drzava_koda": "SI", "trr": "SI56191000000123438",
        "logotip": "logo.png", "ddv_zavezanec": True,
        "email_racunovodja": "racunovodja@example.com",
        "email_posiljatelj": "racuni@example.com",
        "keychain_kljuc_posiljatelja": "alfa", "rok_placila_dni_privzeto": 23,
    }
    data.update(changes)
    return Company.objects.create(**data)


def make_hierarchy(**lease_changes):
    unit = lease_changes.get("unit")
    tenant = lease_changes.get("tenant")
    if unit is None:
        company = make_company()
        property_ = Property.objects.create(company=company, naziv="Stavba", naslov="A")
        unit = Unit.objects.create(
            property=property_, oznaka="P1", tip_enote=Unit.TipEnote.PISARNA
        )
    if tenant is None:
        tenant = Tenant.objects.create(
            tip=Tenant.Tip.FIZICNA_OSEBA, naziv="Janez Novak", naslov="B",
            drzava_koda="SI", identificiran_za_ddv=False,
            email="janez@example.com",
        )
    data = {
        "unit": unit, "tenant": tenant, "oznaka_pogodbe": "P-1",
        "tip_trajanja": Lease.TipTrajanja.NEDOLOCEN_CAS,
        "datum_zacetka": date(2026, 1, 1), "rok_placila_dni": 15,
    }
    data.update(lease_changes)
    return Lease.objects.create(**data)


def make_vat(sifra="DDV22", aktiven=True, zahteva_referenco=False):
    return VatRegime.objects.create(
        zahteva_polni_odbitek=False,
        sifra=sifra, naziv=sifra,
        nacin_obracuna=VatRegime.NacinObracuna.OBDAVCENO,
        stopnja=Decimal("22.00"), rekapitulacijska_skupina=sifra,
        zahteva_referenco=zahteva_referenco, velja_od=date(2020, 1, 1),
        aktiven=aktiven,
    )


def lease_data(lease, **changes):
    data = {
        "edit_fingerprint": lease_source_fingerprint(lease) if lease.pk else "",
        "razlog": "Aneks",
        "unit": lease.unit_id, "tenant": lease.tenant_id,
        "oznaka_pogodbe": lease.oznaka_pogodbe,
        "tip_trajanja": Lease.TipTrajanja.NEDOLOCEN_CAS,
        "datum_zacetka": "2026-01-01", "datum_konca": "",
        "samodejno_podaljsanje": "", "obdobje_podaljsanja_mesecev": "",
        "odpovedni_rok_mesecev": "", "datum_odpovedi": "",
        "datum_prenehanja": "", "status": Lease.Status.AKTIVNA,
        "rok_placila_dni": "15", "ima_klavzulo_indeksacije": "",
        "opis_pogoja_indeksacije": "", "tip_pogoja_indeksacije": "",
        "prag_spremembe_odstotek": "",
    }
    data.update(changes)
    return data


def item_data(vat, **changes):
    data = {
        "naziv_postavke": "Najemnina", "znesek_neto_mesecno": "100.00",
        "vat_regime": vat.pk, "referenca_listine": "", "datum_reference": "",
        "datum_zacetka_postavke": "", "datum_prenehanja_postavke": "",
        "indeks_ob_osnovi": "", "datum_osnove_indeksacije": "",
        "osnovna_najemnina_ob_sklenitvi": "",
    }
    data.update(changes)
    return data


@pytest.mark.django_db
def test_lease_list_empty_state_and_distinct_archive_state(client):
    assert "Ni še nobene najemne pogodbe" in client.get("/pogodbe/").content.decode()
    active = make_hierarchy(status=Lease.Status.PRENEHALA)
    archived = make_hierarchy(
        unit=active.unit, tenant=active.tenant, oznaka_pogodbe="P-2", arhivirana=True
    )
    content = client.get("/pogodbe/").content.decode()
    assert active.oznaka_pogodbe in content and archived.oznaka_pogodbe in content
    assert "Prenehala" in content and "Arhivirana" in content
    assert content.index(active.oznaka_pogodbe) < content.index(archived.oznaka_pogodbe)


@pytest.mark.django_db
def test_new_lease_choices_filter_inactive_and_reject_injection(client):
    usable = make_hierarchy()
    inactive_unit = Unit.objects.create(
        property=usable.unit.property, oznaka="X", tip_enote=Unit.TipEnote.DRUGO,
        aktivna=False,
    )
    inactive_tenant = Tenant.objects.create(
        tip=Tenant.Tip.FIZICNA_OSEBA, naziv="Neaktiven", naslov="X",
        drzava_koda="SI", identificiran_za_ddv=False, email="n@example.com",
        aktiven=False,
    )
    usable.delete()
    page = client.get("/pogodbe/nova/").content.decode()
    assert usable.unit.oznaka in page and "Neaktiven" not in page
    assert f'<option value="{inactive_unit.pk}">' not in page
    assert 'name="arhivirana"' not in page
    response = client.post("/pogodbe/nova/", lease_data(
        usable, unit=inactive_unit.pk, tenant=inactive_tenant.pk,
    ))
    assert response.status_code == 200


@pytest.mark.django_db
@pytest.mark.parametrize(("days", "expected"), [("", 23), ("41", 41)])
def test_new_lease_payment_days_default_or_custom(client, days, expected):
    lease = make_hierarchy()
    lease.delete()
    response = client.post("/pogodbe/nova/", lease_data(lease, rok_placila_dni=days))
    created = Lease.objects.get()
    assert response.status_code == 302 and created.rok_placila_dni == expected


@pytest.mark.django_db
def test_existing_lease_rejects_blank_payment_days_without_changing_value(client):
    lease = make_hierarchy(rok_placila_dni=27)

    response = client.post(
        f"/pogodbe/{lease.pk}/",
        lease_data(lease, rok_placila_dni=""),
    )

    lease.refresh_from_db()
    assert response.status_code == 200
    assert lease.rok_placila_dni == 27
    assert "Rok plačila je obvezen pri urejanju obstoječe pogodbe." in response.content.decode()


@pytest.mark.django_db
@pytest.mark.parametrize(("changes", "field"), [
    ({"tip_trajanja": Lease.TipTrajanja.DOLOCEN_CAS}, "datum_konca"),
    ({"datum_konca": "2027-01-01"}, "datum_konca"),
    ({"samodejno_podaljsanje": "on"}, "obdobje_podaljsanja_mesecev"),
    ({"ima_klavzulo_indeksacije": "on"}, "tip_pogoja_indeksacije"),
    ({"ima_klavzulo_indeksacije": "on", "tip_pogoja_indeksacije": Lease.TipPogojaIndeksacije.PRAG_SPREMEMBE}, "prag_spremembe_odstotek"),
])
def test_lease_model_validation_is_inline(client, changes, field):
    lease = make_hierarchy()
    lease.delete()
    response = client.post("/pogodbe/nova/", lease_data(lease, **changes))
    assert response.status_code == 200
    assert response.context["form"].errors[field]
    assert "field-error" in response.content.decode()


@pytest.mark.django_db
def test_lease_edit_archive_reactivate_and_parent_failure(client):
    lease = make_hierarchy()
    response = client.post(f"/pogodbe/{lease.pk}/", lease_data(
        lease, oznaka_pogodbe="P-NEW", rok_placila_dni="33", arhivirana="on",
    ))
    lease.refresh_from_db()
    assert response.status_code == 302 and (lease.oznaka_pogodbe, lease.rok_placila_dni, lease.arhivirana) == ("P-NEW", 33, False)
    assert client.get(f"/pogodbe/{lease.pk}/arhiviraj/").status_code == 405
    client.post(f"/pogodbe/{lease.pk}/arhiviraj/")
    lease.unit.aktivna = False
    save_business_record(lease.unit, update_fields=["aktivna"])
    blocked = client.post(
        f"/pogodbe/{lease.pk}/reaktiviraj/",
        lease_data(lease, oznaka_pogodbe="VSAJENO", rok_placila_dni="99"),
    )
    lease.refresh_from_db()
    assert blocked.status_code == 200 and lease.arhivirana is True
    assert (lease.oznaka_pogodbe, lease.rok_placila_dni) == ("P-NEW", 33)
    assert blocked.context["form"].is_bound is False
    assert blocked.context["form"].initial["oznaka_pogodbe"] == "P-NEW"
    assert not blocked.context["form"].errors
    assert "ni aktiven" in blocked.content.decode()
    lease.unit.aktivna = True
    save_business_record(lease.unit, update_fields=["aktivna"])
    client.post(f"/pogodbe/{lease.pk}/reaktiviraj/")
    lease.refresh_from_db()
    assert lease.arhivirana is False


@pytest.mark.django_db
def test_item_create_server_owns_lease_and_filters_vat(client):
    lease = make_hierarchy()
    other = make_hierarchy(unit=lease.unit, tenant=lease.tenant, oznaka_pogodbe="P-2")
    active = make_vat()
    inactive = make_vat("STAR", False)
    page = client.get(f"/pogodbe/{lease.pk}/postavke/nova/").content.decode()
    assert "DDV22" in page and "STAR" not in page
    assert 'name="lease"' not in page and 'name="arhivirana"' not in page
    response = client.post(f"/pogodbe/{lease.pk}/postavke/nova/", item_data(
        active, lease=other.pk,
    ))
    item = LeaseItem.objects.get()
    assert response.status_code == 302 and item.lease == lease
    assert item.naziv_postavke in client.get(f"/pogodbe/{lease.pk}/").content.decode()
    rejected = client.post(f"/pogodbe/{lease.pk}/postavke/nova/", item_data(inactive))
    assert rejected.status_code == 200 and LeaseItem.objects.count() == 1


@pytest.mark.django_db
def test_item_create_parent_guard_and_model_validation(client):
    lease = make_hierarchy()
    required = make_vat(zahteva_referenco=True)
    invalid = client.post(f"/pogodbe/{lease.pk}/postavke/nova/", item_data(required))
    assert "Referenca listine je obvezna" in invalid.content.decode()
    invalid = client.post(f"/pogodbe/{lease.pk}/postavke/nova/", item_data(required, znesek_neto_mesecno="0"))
    assert "znesek_neto_mesecno" in invalid.context["form"].errors
    assert "field-error" in invalid.content.decode()
    lease.arhivirana = True
    save_financial_fixture(lease, update_fields=["arhivirana"])
    blocked = client.post(f"/pogodbe/{lease.pk}/postavke/nova/", item_data(required))
    assert blocked.status_code == 200 and "ni aktiven" in blocked.content.decode()
    assert LeaseItem.objects.count() == 0


@pytest.mark.django_db
def test_indexed_lease_requires_item_index_fields(client):
    lease = make_hierarchy(
        ima_klavzulo_indeksacije=True,
        tip_pogoja_indeksacije=Lease.TipPogojaIndeksacije.VEDNO,
    )
    response = client.post(
        f"/pogodbe/{lease.pk}/postavke/nova/", item_data(make_vat())
    )
    assert "Izhodiščni indeks je obvezen" in response.content.decode()
    assert "Datum osnove indeksacije je obvezen" in response.content.decode()


@pytest.mark.django_db
def test_item_metadata_edit_keeps_audited_values_and_filters_inactive_vat(client):
    lease = make_hierarchy()
    current = make_vat("STAR", False)
    make_vat("DRUG", False)
    item = LeaseItem.objects.create(
        lease=lease, naziv_postavke="Najemnina", znesek_neto_mesecno=Decimal(100),
        vat_regime=current,
    )
    page = client.get(f"/pogodbe/postavke/{item.pk}/").content.decode()
    assert "STAR" in page and "DRUG" not in page
    assert 'name="znesek_neto_mesecno"' not in page
    response = client.post(f"/pogodbe/postavke/{item.pk}/", {
        "naziv_postavke": "Parkiranje", "vat_regime": current.pk,
        "edit_fingerprint": record_edit_fingerprint(item), "razlog": "Aneks",
        "referenca_listine": "", "datum_reference": "",
        "indeks_ob_osnovi": "", "datum_osnove_indeksacije": "",
        "osnovna_najemnina_ob_sklenitvi": "", "znesek_neto_mesecno": "999",
        "datum_zacetka_postavke": "2027-01-01",
    })
    item.refresh_from_db()
    assert response.status_code == 302
    assert item.naziv_postavke == "Parkiranje"
    assert item.znesek_neto_mesecno == Decimal("100.00")
    assert item.datum_zacetka_postavke is None


@pytest.mark.django_db
def test_item_correction_requires_reason_audits_all_fields_and_rolls_back(client):
    lease = make_hierarchy()
    item = LeaseItem.objects.create(
        lease=lease, znesek_neto_mesecno=Decimal(100), vat_regime=make_vat()
    )
    url = f"/pogodbe/postavke/{item.pk}/popravek/"
    assert client.post(url, {
        "znesek_neto_mesecno": "120", "datum_zacetka_postavke": "",
        "datum_prenehanja_postavke": "", "razlog": "",
    }).status_code == 200
    assert not AuditLogEntry.objects.exists()
    response = client.post(url, {
        "edit_fingerprint": record_edit_fingerprint(item),
        "znesek_neto_mesecno": "120", "datum_zacetka_postavke": "2026-02-01",
        "datum_prenehanja_postavke": "2026-12-31", "razlog": "Aneks",
    })
    item.refresh_from_db()
    audits = AuditLogEntry.objects.filter(
        content_type=ContentType.objects.get_for_model(LeaseItem), object_id=item.pk,
    )
    assert response.status_code == 302 and audits.count() == 3
    assert set(audits.values_list("polje", flat=True)) == {
        "znesek_neto_mesecno", "datum_zacetka_postavke",
        "datum_prenehanja_postavke",
    }
    before = audits.count()
    invalid = client.post(url, {
        "edit_fingerprint": record_edit_fingerprint(item),
        "znesek_neto_mesecno": "0", "datum_zacetka_postavke": "2027-01-01",
        "datum_prenehanja_postavke": "", "razlog": "Napačno",
    })
    item.refresh_from_db()
    assert invalid.status_code == 200 and item.znesek_neto_mesecno == Decimal(120)
    assert audits.count() == before


@pytest.mark.django_db
def test_item_archive_reactivate_and_parent_failure(client):
    lease = make_hierarchy()
    item = LeaseItem.objects.create(
        lease=lease, znesek_neto_mesecno=Decimal(100), vat_regime=make_vat()
    )
    assert client.get(f"/pogodbe/postavke/{item.pk}/arhiviraj/").status_code == 405
    client.post(f"/pogodbe/postavke/{item.pk}/arhiviraj/")
    lease.arhivirana = True
    save_financial_fixture(lease, update_fields=["arhivirana"])
    blocked = client.post(
        f"/pogodbe/postavke/{item.pk}/reaktiviraj/",
        {
            "naziv_postavke": "VSAJENO",
            "vat_regime": item.vat_regime_id,
            "referenca_listine": "VSAJENO",
        },
    )
    item.refresh_from_db()
    assert blocked.status_code == 200 and item.arhivirana is True
    assert (item.naziv_postavke, item.referenca_listine) == ("Najemnina", "")
    assert blocked.context["form"].is_bound is False
    assert blocked.context["form"].initial["naziv_postavke"] == "Najemnina"
    assert not blocked.context["form"].errors
    assert "ni aktiven" in blocked.content.decode()
    lease.arhivirana = False
    save_financial_fixture(lease, update_fields=["arhivirana"])
    client.post(f"/pogodbe/postavke/{item.pk}/reaktiviraj/")
    item.refresh_from_db()
    assert item.arhivirana is False


@pytest.mark.django_db(transaction=True)
def test_security_no_delete_invalid_ids_csrf_and_monthly_isolation(client):
    lease = make_hierarchy()
    cycle = shrani_prvi_korak(date(2031, 2, 1), date(2031, 2, 12))
    before = list(MonthlyBatch.objects.filter(cycle=cycle).values_list("pk", "company_id"))
    client.post(f"/pogodbe/{lease.pk}/", lease_data(lease, oznaka_pogodbe="P-X"))
    assert list(MonthlyBatch.objects.filter(cycle=cycle).values_list("pk", "company_id")) == before
    for url in ("/pogodbe/999999/", "/pogodbe/postavke/999999/", "/pogodbe/postavke/999999/popravek/"):
        assert client.get(url).status_code == 404
    for name in ("pogodba_izbrisi", "postavka_izbrisi"):
        with pytest.raises(NoReverseMatch):
            reverse(f"ui:{name}", args=[1])
    csrf_client = Client(enforce_csrf_checks=True)
    assert csrf_client.post(f"/pogodbe/{lease.pk}/arhiviraj/").status_code == 403
