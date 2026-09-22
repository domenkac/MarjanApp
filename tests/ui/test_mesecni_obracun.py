from datetime import date

import pytest
from django.utils import timezone

from companies.models import Company
from invoicing.models import Invoice


def ustvari_podjetje(naziv, davcna_stevilka, *, aktivno=True):
    return Company.objects.create(
        drzava_koda="SI",
        naziv=naziv,
        davcna_stevilka=davcna_stevilka,
        maticna_stevilka="1234567000",
        naslov="Glavna ulica 1",
        trr="SI56191000000123438",
        logotip="logo.png",
        email_racunovodja="racunovodja@example.com",
        email_posiljatelj="racuni@example.com",
        keychain_kljuc_posiljatelja=f"marjanapp-{davcna_stevilka}",
        aktivno=aktivno,
    )


@pytest.mark.django_db
def test_stran_prikaze_aktiven_prvi_korak_in_vseh_sest_oznak(client):
    response = client.get("/mesecni-obracun/")
    vsebina = response.content.decode()

    assert response.status_code == 200
    assert 'class="page-heading billing-heading"' in vsebina
    assert "dashboard-heading" not in vsebina
    assert 'class="nav-item is-active" href="/" aria-current="page"' in vsebina
    assert 'class="wizard-step is-active" aria-current="step"' in vsebina
    for oznaka in (
        "Izbira meseca",
        "Predhodna kontrola",
        "Izračun osnutkov",
        "Paketni pregled Δ",
        "Potrditev paketa",
        "Zaključek ciklusa",
    ):
        assert oznaka in vsebina
    assert "eSLOG" not in vsebina
    assert "XML" not in vsebina


@pytest.mark.django_db
def test_get_privzeto_uporabi_tekoci_mesec_in_prvi_dan_za_izdajo(client):
    danes = timezone.localdate()

    response = client.get("/mesecni-obracun/")

    assert response.context["form"].initial == {
        "leto": danes.year,
        "mesec": danes.month,
        "datum_izdaje": danes.replace(day=1),
    }
    assert response.context["obdobje_od"] == danes.replace(day=1)
    assert f'value="{danes:%Y-%m}-01"' in response.content.decode()


@pytest.mark.django_db
def test_prikaze_samo_resnicna_aktivna_podjetja(client):
    ustvari_podjetje("Aktivno podjetje d.o.o.", "15012557")
    ustvari_podjetje("Neaktivno podjetje d.o.o.", "69165028", aktivno=False)

    vsebina = client.get("/mesecni-obracun/").content.decode()

    assert "Aktivno podjetje d.o.o." in vsebina
    assert "Neaktivno podjetje d.o.o." not in vsebina
    assert "Alpha Real Estate" not in vsebina
    assert "Beta Properties" not in vsebina
    assert "Aktivna podjetja" in vsebina
    assert "Predhodna kontrola se izvaja na ravni posameznega podjetja." in vsebina
    assert "Vključena so vsa aktivna podjetja." not in vsebina
    assert "obračun vseh aktivnih izdajateljev" not in vsebina


@pytest.mark.django_db
def test_brez_aktivnih_podjetij_prikaze_blokado(client):
    response = client.get("/mesecni-obracun/")

    assert "Ni aktivnih podjetij" in response.content.decode()
    assert b"disabled" in response.content


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("podatki", "napaka"),
    [
        (
            {"leto": "2026", "mesec": "13", "datum_izdaje": "2026-08-01"},
            "Izberite veljaven obračunski mesec.",
        ),
        (
            {"leto": "0", "mesec": "8", "datum_izdaje": "2026-08-01"},
            "Vnesite leto, ki ga koledar podpira.",
        ),
        (
            {"leto": "abc", "mesec": "8", "datum_izdaje": "2026-08-01"},
            "Vnesite veljavno obračunsko leto.",
        ),
    ],
)
def test_neveljaven_mesec_ali_leto_je_zavrnjen(client, podatki, napaka):
    ustvari_podjetje("Aktivno podjetje d.o.o.", "15012557")

    response = client.post("/mesecni-obracun/", podatki)

    assert response.status_code == 200
    assert napaka in response.content.decode()
    assert response.context["nadaljevanje"] is None


@pytest.mark.django_db
def test_neveljaven_datum_izdaje_ohrani_obdobje_iz_oddanega_meseca(client):
    ustvari_podjetje("Aktivno podjetje d.o.o.", "15012557")

    response = client.post(
        "/mesecni-obracun/",
        {"leto": "2031", "mesec": "2", "datum_izdaje": "ni-datum"},
    )

    assert response.context["nadaljevanje"] is None
    assert response.context["obdobje_od"] == date(2031, 2, 1)
    assert response.context["obdobje_do"] == date(2031, 2, 28)
    assert "01. 02. 2031 – 28. 02. 2031" in response.content.decode()


@pytest.mark.django_db
def test_veljavna_izbira_ne_spremeni_baze_in_ne_izvede_naslednjih_korakov(client):
    ustvari_podjetje("Aktivno podjetje d.o.o.", "15012557")
    podjetja_prej = Company.objects.count()

    response = client.post(
        "/mesecni-obracun/",
        {"leto": "2026", "mesec": "8", "datum_izdaje": "2026-08-12"},
    )

    assert response.status_code == 200
    assert response.context["nadaljevanje"] == {
        "leto": 2026,
        "mesec": 8,
        "datum_izdaje": date(2026, 8, 12),
        "obdobje_od": date(2026, 8, 1),
        "obdobje_do": date(2026, 8, 31),
    }
    assert "Predhodna kontrola za 08/2026 še ni implementirana." in response.content.decode()
    assert Company.objects.count() == podjetja_prej
    assert Invoice.objects.count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize("datum_izdaje", ["2024-02-01", "2024-03-12"])
def test_prestopni_februar_ostane_obdobje_storitve_ne_glede_na_datum_izdaje(
    client,
    datum_izdaje,
):
    ustvari_podjetje("Aktivno podjetje d.o.o.", "15012557")

    response = client.post(
        "/mesecni-obracun/",
        {"leto": "2024", "mesec": "2", "datum_izdaje": datum_izdaje},
    )

    assert response.context["nadaljevanje"]["obdobje_od"] == date(2024, 2, 1)
    assert response.context["nadaljevanje"]["obdobje_do"] == date(2024, 2, 29)
    assert "01. 02. 2024 – 29. 02. 2024" in response.content.decode()
