import pytest
from django.template.loader import render_to_string

from companies.models import Company
from invoicing.models import Invoice


@pytest.mark.django_db
def test_prazna_nadzorna_plosca(client):
    response = client.get("/")

    assert response.status_code == 200
    assert response.context["counts"] == {
        "podjetja": 0,
        "najemniki": 0,
        "pogodbe": 0,
        "izdani_racuni": 0,
    }
    vsebina = response.content.decode()
    assert "<h1>Nadzorna plošča</h1>" in vsebina
    assert 'class="page-heading dashboard-heading"' in vsebina
    assert 'aria-current="page"' in vsebina
    assert 'href="/mesecni-obracun/"' in vsebina
    assert "Začni mesečni obračun" in vsebina
    assert "Mesečni obračun in ročne fakture še niso na voljo." not in vsebina
    assert 'class="nav-item is-active" href="/" aria-current="page"' in vsebina
    assert "eSLOG" not in vsebina
    assert "XML" not in vsebina
    assert "Pošiljanje ni vključeno" in vsebina
    assert "Dostava računov bo dodana v kasnejši fazi." in vsebina
    assert "Ni še poslano" not in vsebina


def test_prva_raven_navigacije_sledi_prd():
    vsebina = render_to_string("ui/partials/sidebar.html")

    for oznaka in ("Nadzorna plošča", "Pogodbe", "Arhiv", "Nastavitve"):
        assert oznaka in vsebina
    for oznaka in (
        "Mesečni obračun",
        "Najemniki",
        "Podjetja",
        "Nepremičnine",
        "Šifranti",
        "Nova ročna faktura",
    ):
        assert oznaka not in vsebina


def test_sidebar_lahko_aktivira_drugo_prvoravensko_sekcijo():
    vsebina = render_to_string(
        "ui/partials/sidebar.html",
        {"active_section": "pogodbe"},
    )

    assert 'class="nav-item" href="/"' in vsebina
    assert 'class="nav-item is-disabled is-active" aria-current="page"' in vsebina


def test_osnovna_predloga_ne_potrebuje_dashboard_contexta():
    vsebina = render_to_string("ui/base.html")

    assert "Vsa podjetja" in vsebina
    assert "counts.podjetja" not in vsebina


@pytest.mark.django_db
def test_nadzorna_plosca_prikaze_resnicna_stevila(client):
    company = Company.objects.create(
        drzava_koda="SI",
        naziv="Alfa d.o.o.",
        davcna_stevilka="15012557",
        maticna_stevilka="1234567000",
        naslov="Glavna ulica 1",
        trr="SI56191000000123438",
        logotip="logo.png",
        email_racunovodja="racunovodja@example.com",
        email_posiljatelj="racuni@example.com",
        keychain_kljuc_posiljatelja="marjanapp-alfa",
    )
    Invoice.objects.create(
        company=company,
        document_type=Invoice.DocumentType.NP,
        stevilka="NP-00147",
        datum_izdaje="2026-09-21",
        obdobje_od="2026-09-01",
        obdobje_do="2026-09-30",
    )

    response = client.get("/")

    assert response.context["counts"] == {
        "podjetja": 1,
        "najemniki": 0,
        "pogodbe": 0,
        "izdani_racuni": 1,
    }
    assert "NP-00147" not in response.content.decode()


def test_nadzorna_plosca_nima_pisalnega_endpointa(client):
    assert client.post("/").status_code == 405
