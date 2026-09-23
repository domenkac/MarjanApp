from datetime import date

import pytest
from django.test import Client
from django.urls import NoReverseMatch, reverse

from companies.models import Company
from contracts.models import Property, Tenant, Unit
from invoicing.models import MonthlyBatch
from invoicing.services import shrani_prvi_korak


def company(naziv="Aktivno podjetje", aktivno=True):
    return Company.objects.create(
        naziv=naziv,
        davcna_stevilka="15012557",
        maticna_stevilka="1234567000",
        naslov="Glavna 1",
        drzava_koda="SI",
        trr="SI56191000000123438",
        logotip="logo.png",
        ddv_zavezanec=True,
        email_racunovodja="racunovodja@example.com",
        email_posiljatelj=f"{naziv[:3].lower()}@example.com",
        keychain_kljuc_posiljatelja=naziv,
        aktivno=aktivno,
    )


def tenant_data(**changes):
    data = {
        "tip": Tenant.Tip.FIZICNA_OSEBA,
        "naziv": "Janez Novak",
        "davcna_stevilka": "",
        "ddv_identifikacijska_stevilka": "",
        "maticna_stevilka": "",
        "naslov": "Trg 1",
        "drzava_koda": "SI",
        "identificiran_za_ddv": "",
        "ddv_zavezanec_s_polnim_odbitkom": "",
        "email": "janez@example.com",
    }
    data.update(changes)
    return data


@pytest.mark.django_db
def test_navigation_landing_and_empty_lists(client):
    dashboard = client.get("/").content.decode()
    assert 'href="/pogodbe/"' in dashboard
    landing = client.get("/pogodbe/")
    assert landing.status_code == 200
    assert all(text in landing.content.decode() for text in ("Pogodbe", "Nepremičnine", "Najemniki"))
    assert "Najprej dodajte nepremičnino podjetja." in client.get("/pogodbe/nepremicnine/").content.decode()
    assert "Ni še nobenega najemnika" in client.get("/pogodbe/najemniki/").content.decode()


@pytest.mark.django_db
def test_contracts_page_uses_contracts_topbar_location(client):
    content = client.get("/pogodbe/nepremicnine/").content.decode()

    assert '<div class="location"><span class="material-symbols-outlined" aria-hidden="true">contract</span>Pogodbe</div>' in content
    assert '<div class="location"><span class="material-symbols-outlined" aria-hidden="true">home</span>Nadzorna plošča</div>' not in content


@pytest.mark.django_db
def test_property_create_filters_company_and_hides_state(client):
    active = company()
    inactive = company("Arhivirano podjetje", False)
    page = client.get("/pogodbe/nepremicnine/nova/").content.decode()
    assert active.naziv in page and inactive.naziv not in page
    assert 'name="aktivna"' not in page
    assert client.post("/pogodbe/nepremicnine/nova/", {
        "company": inactive.pk, "naziv": "Napačna", "naslov": "X",
    }).status_code == 200
    response = client.post("/pogodbe/nepremicnine/nova/", {
        "company": active.pk, "naziv": "Stavba", "naslov": "Glavna 1",
    })
    property_ = Property.objects.get()
    assert response.status_code == 302 and property_.aktivna is True


@pytest.mark.django_db
def test_property_edit_list_archive_and_reactivate(client):
    property_ = Property.objects.create(company=company(), naziv="Stavba", naslov="A")
    assert client.post(f"/pogodbe/nepremicnine/{property_.pk}/", {
        "company": property_.company_id, "naziv": "Stavba B", "naslov": "B",
        "aktivna": "",
    }).status_code == 302
    property_.refresh_from_db()
    assert (property_.naziv, property_.naslov, property_.aktivna) == ("Stavba B", "B", True)
    assert client.get(f"/pogodbe/nepremicnine/{property_.pk}/arhiviraj/").status_code == 405
    client.post(f"/pogodbe/nepremicnine/{property_.pk}/arhiviraj/")
    property_.refresh_from_db()
    assert property_.aktivna is False and property_.naziv in client.get("/pogodbe/nepremicnine/").content.decode()
    client.post(f"/pogodbe/nepremicnine/{property_.pk}/reaktiviraj/")
    property_.refresh_from_db()
    assert property_.aktivna is True


@pytest.mark.django_db
def test_unit_nested_creation_and_edit_cannot_change_property(client):
    first = Property.objects.create(company=company(), naziv="Prva", naslov="A")
    second = Property.objects.create(company=first.company, naziv="Druga", naslov="B")
    page = client.get(f"/pogodbe/nepremicnine/{first.pk}/enote/nova/").content.decode()
    assert "Nova enota" in page and 'name="property"' not in page and 'name="aktivna"' not in page
    response = client.post(f"/pogodbe/nepremicnine/{first.pk}/enote/nova/", {
        "oznaka": "P1", "tip_enote": Unit.TipEnote.PISARNA,
        "povrsina_m2": "12.50", "property": second.pk,
    })
    unit = Unit.objects.get()
    assert response.status_code == 302 and unit.property == first
    client.post(f"/pogodbe/enote/{unit.pk}/", {
        "oznaka": "P2", "tip_enote": Unit.TipEnote.SKLADISCE,
        "povrsina_m2": "13.00", "property": second.pk, "aktivna": "",
    })
    unit.refresh_from_db()
    assert unit.property == first and unit.oznaka == "P2" and unit.aktivna is True


@pytest.mark.django_db
def test_inactive_parent_blocks_unit_create_and_reactivation(client):
    property_ = Property.objects.create(company=company(), naziv="Stavba", naslov="A")
    unit = Unit.objects.create(property=property_, oznaka="P1", tip_enote=Unit.TipEnote.PISARNA)
    client.post(f"/pogodbe/enote/{unit.pk}/arhiviraj/")
    client.post(f"/pogodbe/nepremicnine/{property_.pk}/arhiviraj/")
    assert client.get(f"/pogodbe/nepremicnine/{property_.pk}/enote/nova/").status_code == 404
    blocked = client.post(f"/pogodbe/enote/{unit.pk}/reaktiviraj/")
    unit.refresh_from_db()
    assert blocked.status_code == 200 and unit.aktivna is False
    assert "ni aktiven" in blocked.content.decode()


@pytest.mark.django_db
def test_tenant_create_edit_fields_and_model_validation(client):
    page = client.get("/pogodbe/najemniki/nov/").content.decode()
    for hidden in ("aktiven", "eslog_neobvezni_segmenti_override", "eslog_neobvezna_polja"):
        assert f'name="{hidden}"' not in page
    response = client.post("/pogodbe/najemniki/nov/", tenant_data())
    tenant = Tenant.objects.get()
    assert response.status_code == 302 and tenant.aktiven is True
    client.post(f"/pogodbe/najemniki/{tenant.pk}/", tenant_data(naziv="Janez N."))
    tenant.refresh_from_db()
    assert tenant.naziv == "Janez N."


@pytest.mark.django_db
@pytest.mark.parametrize("changes,error", [
    ({"tip": Tenant.Tip.PRAVNA_OSEBA}, "Za pravno osebo je davčna številka obvezna."),
    ({"davcna_stevilka": "12345678"}, "Neveljavna davčna številka"),
    ({"identificiran_za_ddv": "on"}, "Davčna številka je obvezna"),
    ({"drzava_koda": "AT", "identificiran_za_ddv": "on", "davcna_stevilka": "123"}, "Tuja oseba"),
    ({"ddv_zavezanec_s_polnim_odbitkom": "on"}, "Polni odbitek DDV"),
    ({"drzava_koda": "XX"}, "Koda države mora biti veljavna"),
    ({"email": "ni-email"}, "Vnesite veljaven e-poštni naslov"),
])
def test_tenant_validation_is_inline(client, changes, error):
    response = client.post("/pogodbe/najemniki/nov/", tenant_data(**changes))
    assert response.status_code == 200 and error in response.content.decode()
    assert Tenant.objects.count() == 0


@pytest.mark.django_db
def test_tenant_archive_reactivate_and_archived_visible(client):
    tenant = Tenant.objects.create(**{
        **tenant_data(), "identificiran_za_ddv": False,
        "ddv_zavezanec_s_polnim_odbitkom": False,
    })
    assert client.get(f"/pogodbe/najemniki/{tenant.pk}/arhiviraj/").status_code == 405
    client.post(f"/pogodbe/najemniki/{tenant.pk}/arhiviraj/")
    tenant.refresh_from_db()
    assert tenant.aktiven is False and tenant.naziv in client.get("/pogodbe/najemniki/").content.decode()
    client.post(f"/pogodbe/najemniki/{tenant.pk}/reaktiviraj/")
    tenant.refresh_from_db()
    assert tenant.aktiven is True


@pytest.mark.django_db
def test_security_invalid_ids_no_delete_and_monthly_isolation(client):
    company_ = company()
    cycle = shrani_prvi_korak(date(2031, 2, 1), date(2031, 2, 12))
    before = list(MonthlyBatch.objects.filter(cycle=cycle).values_list("pk", "company_id"))
    client.post("/pogodbe/nepremicnine/nova/", {
        "company": company_.pk, "naziv": "Stavba", "naslov": "A",
    })
    client.post("/pogodbe/najemniki/nov/", tenant_data())
    assert list(MonthlyBatch.objects.filter(cycle=cycle).values_list("pk", "company_id")) == before
    for url in ("/pogodbe/nepremicnine/999999/", "/pogodbe/enote/999999/", "/pogodbe/najemniki/999999/"):
        assert client.get(url).status_code == 404
    for name in ("nepremicnina_izbrisi", "enota_izbrisi", "najemnik_izbrisi"):
        with pytest.raises(NoReverseMatch):
            reverse(f"ui:{name}", args=[1])
    csrf_client = Client(enforce_csrf_checks=True)
    assert csrf_client.post("/pogodbe/najemniki/nov/", tenant_data()).status_code == 403
