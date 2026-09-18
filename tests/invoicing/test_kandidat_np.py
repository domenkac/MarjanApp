from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from companies.models import NumberSequence, Signatory
from invoicing.models import Invoice, InvoiceLine
from invoicing.services import (
    izracunaj_kandidata,
    potrdi_osnutek_np,
    preveri_predpogoje_podjetja,
    pripravi_osnutek_np,
)
from tests.invoicing.test_priprava_np import dodaj_postavko

pytest_plugins = ["tests.invoicing.test_priprava_np"]

AVGUST_OD = date(2026, 8, 1)
AVGUST_DO = date(2026, 8, 31)
SEPTEMBER_OD = date(2026, 9, 1)
SEPTEMBER_DO = date(2026, 9, 30)
DATUM_IZDAJE = date(2026, 10, 1)


def _izdaj_avgust(pogodba):
    osnutek = pripravi_osnutek_np(
        pogodba, AVGUST_OD, AVGUST_DO, date(2026, 9, 1)
    ).invoice
    return potrdi_osnutek_np(osnutek)


def test_predpogoji_podjetja_so_strukturirani(pogodba):
    company = pogodba.unit.property.company
    assert preveri_predpogoje_podjetja(company, DATUM_IZDAJE) == []

    company.ddv_razporeditev_potrjena = False
    company.save(update_fields=["ddv_razporeditev_potrjena"])
    Signatory.objects.filter(company=company).delete()

    blokade = preveri_predpogoje_podjetja(company, DATUM_IZDAJE)
    assert {blokada.koda for blokada in blokade} == {
        "DDV_NEPOTRJEN", "PODPISNIK_MANJKA"
    }


@pytest.mark.parametrize("razlog", ["ddv", "podpisnik", "prekrivanje"])
def test_kandidat_vrne_predpogoje_brez_izdaje(pogodba, razlog):
    company = pogodba.unit.property.company
    dodaj_postavko(pogodba)
    if razlog == "ddv":
        company.ddv_razporeditev_potrjena = False
        company.save(update_fields=["ddv_razporeditev_potrjena"])
        koda = "DDV_NEPOTRJEN"
    elif razlog == "podpisnik":
        Signatory.objects.filter(company=company).delete()
        koda = "PODPISNIK_MANJKA"
    else:
        Signatory.objects.create(
            company=company, ime_priimek="Drugi",
            podpis_slika="drugi.png", velja_od=SEPTEMBER_OD,
            velja_do=DATUM_IZDAJE,
        )
        koda = "PODPISNIK_PREKRIVANJE"

    kandidat = izracunaj_kandidata(
        pogodba, SEPTEMBER_OD, SEPTEMBER_DO, DATUM_IZDAJE
    )
    assert kandidat.se_izda is False
    assert koda in {izjema.koda for izjema in kandidat.izjeme}
    assert not Invoice.objects.exists()
    assert not NumberSequence.objects.exists()


def test_brez_aktivnih_postavk_ne_ustvari_praznega_racuna(pogodba):
    dodaj_postavko(pogodba, arhivirana=True)
    kandidat = izracunaj_kandidata(
        pogodba, SEPTEMBER_OD, SEPTEMBER_DO, DATUM_IZDAJE
    )
    assert kandidat.se_izda is False
    assert kandidat.vrstice == ()
    assert kandidat.razlog_neizdaje
    assert not Invoice.objects.exists()
    assert not NumberSequence.objects.exists()


def test_obicajen_celomescni_kandidat_brez_izjem_in_stranskih_ucinkov(pogodba):
    postavka = dodaj_postavko(pogodba)
    _izdaj_avgust(pogodba)
    stevilo_racunov = Invoice.objects.count()
    zaporedna = NumberSequence.objects.get().zadnja_stevilka

    kandidat = izracunaj_kandidata(
        pogodba, SEPTEMBER_OD, SEPTEMBER_DO, DATUM_IZDAJE
    )

    assert kandidat.se_izda is True
    assert kandidat.skupaj_z_ddv == Decimal("122.00")
    assert kandidat.izjeme == ()
    assert len(kandidat.vrstice) == 1
    assert kandidat.vrstice[0].lease_item_id == postavka.pk
    assert kandidat.vrstice[0].je_sorazmerno is False
    assert kandidat.vrstice[0].pk is None
    assert Invoice.objects.count() == stevilo_racunov
    assert InvoiceLine.objects.count() == 1
    assert NumberSequence.objects.get().zadnja_stevilka == zaporedna


def test_manjkajoca_obvezna_referenca_ostane_blokada(pogodba):
    postavka = dodaj_postavko(pogodba)
    postavka.vat_regime.zahteva_referenco = True
    postavka.vat_regime.save(update_fields=["zahteva_referenco"])

    with pytest.raises(ValidationError) as error:
        izracunaj_kandidata(
            pogodba, SEPTEMBER_OD, SEPTEMBER_DO, DATUM_IZDAJE
        )
    assert {"referenca_listine", "datum_reference"} <= set(error.value.message_dict)
    assert not Invoice.objects.exists()


def test_sorazmerno_vedno_sprozi_izjemo(pogodba):
    postavka = dodaj_postavko(pogodba, znesek="0.01")
    _izdaj_avgust(pogodba)
    postavka.datum_prenehanja_postavke = date(2026, 9, 29)
    postavka.save(update_fields=["datum_prenehanja_postavke"])

    kandidat = izracunaj_kandidata(
        pogodba, SEPTEMBER_OD, SEPTEMBER_DO, DATUM_IZDAJE
    )
    assert kandidat.se_izda is True
    assert kandidat.skupaj_z_ddv == Decimal("0.01")
    assert kandidat.vrstice[0].je_sorazmerno is True
    assert [(e.koda, e.lease_item_id) for e in kandidat.izjeme] == [
        ("SORAZMERNO", postavka.pk)
    ]


def test_ena_sorazmerna_med_vec_postavkami_ena_izjema(pogodba):
    polna = dodaj_postavko(pogodba)
    delna = dodaj_postavko(pogodba, naziv="Parkirišče", znesek="30.00", nacin="OPROSCENO")
    _izdaj_avgust(pogodba)
    delna.datum_prenehanja_postavke = date(2026, 9, 15)
    delna.save(update_fields=["datum_prenehanja_postavke"])

    kandidat = izracunaj_kandidata(
        pogodba, SEPTEMBER_OD, SEPTEMBER_DO, DATUM_IZDAJE
    )
    assert kandidat.se_izda is True
    assert {v.lease_item_id for v in kandidat.vrstice} == {polna.pk, delna.pk}
    assert kandidat.skupaj_z_ddv == Decimal("137.00")
    assert [(e.koda, e.lease_item_id) for e in kandidat.izjeme] == [
        ("SORAZMERNO", delna.pk)
    ]