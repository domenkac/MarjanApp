from datetime import date
from decimal import Decimal

import pytest
from django.test import Client
from django.urls import NoReverseMatch, reverse

from catalogs.models import ServiceCatalogItem, VatRegime
from companies.models import Company, Signatory
from companies.services import potrdi_ddv_nastavitve
from contracts.models import Lease, LeaseItem, Property, Tenant, Unit
from core.models import AuditLogEntry
from core.services import record_edit_fingerprint
from invoicing.models import MonthlyBatch
from invoicing.services import izracunaj_kandidata, shrani_prvi_korak


def vat_data(**changes):
    data = {
        "sifra": "DDV22",
        "naziv": "DDV 22 %",
        "nacin_obracuna": VatRegime.NacinObracuna.OBDAVCENO,
        "stopnja": "22.00",
        "klavzula": "",
        "rekapitulacijska_skupina": "DDV 22 %",
        "zahteva_referenco": "",
        "zahteva_polni_odbitek": "False",
        "velja_od": "2026-01-01",
        "velja_do": "",
    }
    data.update(changes)
    return data


def create_vat(**changes):
    data = vat_data()
    data["stopnja"] = Decimal(data["stopnja"])
    data["zahteva_referenco"] = bool(data["zahteva_referenco"])
    data["zahteva_polni_odbitek"] = data["zahteva_polni_odbitek"] == "True"
    data["velja_od"] = date.fromisoformat(data["velja_od"])
    data["velja_do"] = (
        date.fromisoformat(data["velja_do"]) if data["velja_do"] else None
    )
    data.update(changes)
    return VatRegime.objects.create(**data)


@pytest.mark.django_db
def test_settings_navigation_and_empty_catalog_lists(client):
    for url, empty_text in (
        ("/nastavitve/", "Dodaj podjetje"),
        ("/nastavitve/ddv/", "Dodajte prvi DDV režim"),
        ("/nastavitve/storitve/", "Dodajte prvo storitev"),
    ):
        response = client.get(url)
        content = response.content.decode()
        assert response.status_code == 200
        assert empty_text in content
        assert "Podjetja" in content
        assert "DDV režimi" in content
        assert "Storitve" in content


@pytest.mark.django_db
def test_vat_list_orders_active_first_and_form_hides_internal_fields(client):
    create_vat(sifra="NEAKT", naziv="Neaktiven", aktiven=False)
    active = create_vat(sifra="AKT", naziv="Aktiven")

    content = client.get("/nastavitve/ddv/").content.decode()
    assert content.index("Aktiven") < content.index("Neaktiven")
    assert "is-archived" in content
    assert "+ Dodaj DDV režim" in content

    form = client.get("/nastavitve/ddv/nov/").content.decode()
    assert "Skupina za rekapitulacijo DDV" in form
    assert "Postavke z isto skupino se na računu seštejejo v eno vrstico." in form
    assert "besedilo, ki se po potrebi izpiše na računu" in form
    assert "podatki reference oziroma listine" in form
    assert 'name="aktiven"' not in form
    assert 'name="eslog_koda"' not in form
    assert 'type="date"' in form

    edit_form = client.get(f"/nastavitve/ddv/{active.pk}/").content.decode()
    assert 'name="eslog_koda"' not in edit_form


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("nacin", "koda", "klavzula"),
    [
        (VatRegime.NacinObracuna.OBDAVCENO, "S", ""),
        (VatRegime.NacinObracuna.OPROSCENO, "E", "Oprostitev"),
        (VatRegime.NacinObracuna.OBRNJENA_DAVCNA, "AE", "Obrnjena obveznost"),
        (VatRegime.NacinObracuna.NI_PREDMET, "O", ""),
    ],
)
def test_vat_create_sets_eslog_code(client, nacin, koda, klavzula):
    response = client.post(
        "/nastavitve/ddv/nov/",
        vat_data(nacin_obracuna=nacin, stopnja="0.00", klavzula=klavzula),
    )

    assert response.status_code == 302
    assert VatRegime.objects.get().eslog_koda == koda


@pytest.mark.django_db
def test_vat_create_edit_dates_and_eslog_code(client):
    response = client.post(
        "/nastavitve/ddv/nov/",
        vat_data(velja_do="2027-12-31", zahteva_referenco="on"),
    )
    regime = VatRegime.objects.get(sifra="DDV22")
    assert response.status_code == 302
    assert response.url == f"/nastavitve/ddv/{regime.pk}/"
    assert regime.velja_od == date(2026, 1, 1)
    assert regime.velja_do == date(2027, 12, 31)
    assert regime.zahteva_referenco is True
    assert regime.eslog_koda == "S"
    assert regime.naziv in client.get("/nastavitve/ddv/").content.decode()

    edit = client.post(
        f"/nastavitve/ddv/{regime.pk}/",
        vat_data(
            naziv="Spremenjeni DDV",
            edit_fingerprint=record_edit_fingerprint(regime), razlog="Popravek",
            nacin_obracuna=VatRegime.NacinObracuna.OBDAVCENO,
            stopnja="0.00",
            klavzula="Oprostitev",
            velja_do="2028-06-30",
        ),
    )
    regime.refresh_from_db()
    assert edit.status_code == 302
    assert regime.naziv == "Spremenjeni DDV"
    assert regime.velja_do == date(2028, 6, 30)
    assert regime.eslog_koda == "S"
    assert regime.nacin_obracuna == "OBDAVCENO"


@pytest.mark.django_db
@pytest.mark.parametrize("stara_koda", ["", "E"])
def test_vat_edit_repairs_missing_or_wrong_eslog_code(client, stara_koda):
    regime = create_vat(eslog_koda=stara_koda)

    response = client.post(
        f"/nastavitve/ddv/{regime.pk}/",
        vat_data(edit_fingerprint=record_edit_fingerprint(regime), razlog="Popravek eSLOG kode"),
    )

    regime.refresh_from_db()
    assert response.status_code == 302
    assert regime.eslog_koda == "S"
    entry = AuditLogEntry.objects.get(polje="eslog_koda")
    assert (entry.stara_vrednost, entry.nova_vrednost) == (stara_koda, "S")


@pytest.mark.django_db
def test_ui_created_taxable_vat_works_in_candidate_path(client):
    assert client.post("/nastavitve/ddv/nov/", vat_data()).status_code == 302
    regime = VatRegime.objects.get()
    item = create_active_lease_item(regime)
    company = item.lease.unit.property.company
    Signatory.objects.create(
        company=company,
        ime_priimek="Ana Podpisnica",
        podpis_slika="podpis.png",
        velja_od=date(2020, 1, 1),
    )
    potrdi_ddv_nastavitve(company)

    candidate = izracunaj_kandidata(
        item.lease, date(2026, 1, 1), date(2026, 1, 31), date(2026, 2, 1)
    )

    assert candidate.se_izda is True
    assert candidate.razlog_neizdaje is None
    assert candidate.vrstice[0].ddv_eslog_koda == "S"


@pytest.mark.django_db
@pytest.mark.parametrize(
    "nacin",
    [VatRegime.NacinObracuna.OPROSCENO, VatRegime.NacinObracuna.OBRNJENA_DAVCNA],
)
def test_vat_clause_validation_is_inline(client, nacin):
    response = client.post(
        "/nastavitve/ddv/nov/", vat_data(nacin_obracuna=nacin, stopnja="0.00")
    )
    assert response.status_code == 200
    assert "Klavzula je obvezna za izbrani način obračuna." in response.content.decode()
    assert VatRegime.objects.count() == 0


@pytest.mark.django_db
def test_taxable_vat_does_not_require_clause(client):
    assert client.post("/nastavitve/ddv/nov/", vat_data()).status_code == 302


def create_active_lease_item(regime):
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
    )
    property_ = Property.objects.create(company=company, naziv="Stavba", naslov="Glavna 1")
    unit = Unit.objects.create(
        property=property_, oznaka="P1", tip_enote=Unit.TipEnote.PISARNA
    )
    tenant = Tenant.objects.create(
        tip=Tenant.Tip.FIZICNA_OSEBA,
        naziv="Janez Novak",
        naslov="Trg 1",
        drzava_koda="SI",
        identificiran_za_ddv=False,
        email="janez@example.com",
    )
    lease = Lease.objects.create(
        unit=unit,
        tenant=tenant,
        oznaka_pogodbe="P-1",
        tip_trajanja=Lease.TipTrajanja.NEDOLOCEN_CAS,
        datum_zacetka=date(2020, 1, 1),
        rok_placila_dni=15,
    )
    return LeaseItem.objects.create(
        lease=lease,
        znesek_neto_mesecno=Decimal("100.00"),
        vat_regime=regime,
    )


@pytest.mark.django_db
def test_vat_deactivate_blocked_when_used_and_reactivation_works(client):
    unused = create_vat(sifra="PROST", naziv="Prost")
    assert client.get(f"/nastavitve/ddv/{unused.pk}/deaktiviraj/").status_code == 405
    assert client.post(f"/nastavitve/ddv/{unused.pk}/deaktiviraj/").status_code == 302
    unused.refresh_from_db()
    assert unused.aktiven is False
    assert client.post(f"/nastavitve/ddv/{unused.pk}/aktiviraj/").status_code == 302
    unused.refresh_from_db()
    assert unused.aktiven is True

    used = create_vat(sifra="RABLJEN", naziv="Rabljen")
    create_active_lease_item(used)
    blocked = client.post(f"/nastavitve/ddv/{used.pk}/deaktiviraj/")
    used.refresh_from_db()
    assert blocked.status_code == 200
    assert used.aktiven is True
    assert "DDV režima ni mogoče deaktivirati med aktivno uporabo." in blocked.content.decode()


@pytest.mark.django_db
def test_service_catalog_create_edit_state_and_vat_label(client):
    regime = create_vat()
    create_page = client.get("/nastavitve/storitve/nova/").content.decode()
    assert "DDV22 — DDV 22 %" in create_page
    assert 'name="aktivna"' not in create_page

    response = client.post(
        "/nastavitve/storitve/nova/",
        {
            "naziv": "Svetovanje",
            "privzeta_cena": "125.50",
            "privzeti_ddv_rezim": regime.pk,
            "enota_mere": "ura",
        },
    )
    item = ServiceCatalogItem.objects.get()
    assert response.status_code == 302
    assert response.url == f"/nastavitve/storitve/{item.pk}/"
    assert (item.privzeta_cena, item.privzeti_ddv_rezim, item.enota_mere) == (
        Decimal("125.50"), regime, "ura"
    )

    edit = client.post(
        f"/nastavitve/storitve/{item.pk}/",
        {
            "naziv": "Upravljanje",
            "privzeta_cena": "130.00",
            "privzeti_ddv_rezim": regime.pk,
            "enota_mere": "mesec",
        },
    )
    item.refresh_from_db()
    assert edit.status_code == 302
    assert (item.naziv, item.privzeta_cena, item.enota_mere) == (
        "Upravljanje", Decimal("130.00"), "mesec"
    )
    assert client.get(f"/nastavitve/storitve/{item.pk}/deaktiviraj/").status_code == 405
    assert client.post(f"/nastavitve/storitve/{item.pk}/deaktiviraj/").status_code == 302
    item.refresh_from_db()
    assert item.aktivna is False
    active_item = ServiceCatalogItem.objects.create(
        naziv="Aktivna storitev", enota_mere="kos"
    )
    list_content = client.get("/nastavitve/storitve/").content.decode()
    assert "Neaktivna" in list_content and "is-archived" in list_content
    assert list_content.index(active_item.naziv) < list_content.index(item.naziv)
    assert client.post(f"/nastavitve/storitve/{item.pk}/aktiviraj/").status_code == 302
    item.refresh_from_db()
    assert item.aktivna is True


@pytest.mark.django_db(transaction=True)
def test_service_default_change_does_not_change_lease_item_or_cycle_membership(client):
    old_regime = create_vat(sifra="STAR", naziv="Star")
    new_regime = create_vat(sifra="NOV", naziv="Nov")
    lease_item = create_active_lease_item(old_regime)
    service = ServiceCatalogItem.objects.create(
        naziv="Najemnina",
        privzeta_cena=Decimal("100.00"),
        privzeti_ddv_rezim=old_regime,
        enota_mere="mesec",
    )
    cycle = shrani_prvi_korak(date(2031, 2, 1), date(2031, 2, 12))
    before = list(MonthlyBatch.objects.filter(cycle=cycle).values_list("pk", "company_id"))

    client.post(
        f"/nastavitve/ddv/{old_regime.pk}/",
        vat_data(sifra="STAR", naziv="Spremenjen star", stopnja="20.00"),
    )

    client.post(
        f"/nastavitve/storitve/{service.pk}/",
        {
            "naziv": "Najemnina",
            "privzeta_cena": "200.00",
            "privzeti_ddv_rezim": new_regime.pk,
            "enota_mere": "mesec",
        },
    )
    lease_item.refresh_from_db()
    assert lease_item.vat_regime == old_regime
    assert lease_item.znesek_neto_mesecno == Decimal("100.00")
    assert list(MonthlyBatch.objects.filter(cycle=cycle).values_list("pk", "company_id")) == before


@pytest.mark.django_db
def test_catalog_mutations_require_csrf():
    regime = create_vat()
    csrf_client = Client(enforce_csrf_checks=True)
    assert csrf_client.post(f"/nastavitve/ddv/{regime.pk}/deaktiviraj/").status_code == 403
    assert csrf_client.post("/nastavitve/storitve/nova/", {"naziv": "X"}).status_code == 403


@pytest.mark.django_db
def test_catalog_invalid_ids_and_no_delete_routes(client):
    for url in (
        "/nastavitve/ddv/999999/",
        "/nastavitve/ddv/999999/deaktiviraj/",
        "/nastavitve/ddv/999999/aktiviraj/",
        "/nastavitve/storitve/999999/",
        "/nastavitve/storitve/999999/deaktiviraj/",
        "/nastavitve/storitve/999999/aktiviraj/",
    ):
        response = client.post(url) if url.endswith(("deaktiviraj/", "aktiviraj/")) else client.get(url)
        assert response.status_code == 404
    for name in ("ddv_rezim_izbrisi", "storitev_izbrisi"):
        with pytest.raises(NoReverseMatch):
            reverse(f"ui:{name}", args=[1])
