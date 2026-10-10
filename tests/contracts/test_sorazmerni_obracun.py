from datetime import date
from decimal import Decimal

import pytest

from catalogs.models import VatRegime
from companies.models import Company
from contracts.models import Lease, LeaseItem, Property, Tenant, Unit
from contracts.services import (
    dnevi_v_obdobju,
    efektivna_veljavnost_pogodbe,
    efektivna_veljavnost_postavke,
    izracunaj_osnovo,
    izracunaj_postavko,
)


def postavka(**spremembe):
    podjetje = Company()
    nepremicnina = Property(company=podjetje)
    enota = Unit(property=nepremicnina)
    najemnik = Tenant(tip=Tenant.Tip.PRAVNA_OSEBA, identificiran_za_ddv=False)
    pogodba = Lease(
        unit=enota,
        tenant=najemnik,
        datum_zacetka=date(2026, 1, 1),
        status=Lease.Status.AKTIVNA,
    )
    rezim = VatRegime(
        sifra="TEST_STANDARD",
        zahteva_polni_odbitek=False,
        nacin_obracuna=VatRegime.NacinObracuna.OBDAVCENO,
        stopnja=Decimal("22.00"),
        klavzula="",
        rekapitulacijska_skupina="DDV 22 %",
    )
    return LeaseItem(
        **(
            {
                "lease": pogodba,
                "vat_regime": rezim,
                "znesek_neto_mesecno": Decimal("310.00"),
            }
            | spremembe
        )
    )


@pytest.mark.parametrize(
    ("zacetek", "konec", "pricakovano"),
    [
        (date(2026, 1, 1), date(2026, 1, 31), 31),
        (date(2026, 1, 15), date(2026, 1, 15), 1),
        (date(2025, 12, 1), date(2025, 12, 31), 0),
        (date(2026, 2, 1), None, 0),
        (None, None, 31),
    ],
)
def test_dnevi_v_obdobju_so_inclusive(zacetek, konec, pricakovano):
    assert dnevi_v_obdobju(date(2026, 1, 1), date(2026, 1, 31), zacetek, konec) == pricakovano


@pytest.mark.parametrize(
    ("status", "datum_konca", "datum_prenehanja", "pricakovani_konec"),
    [
        (Lease.Status.AKTIVNA, date(2026, 6, 30), date(2026, 3, 15), date(2026, 6, 30)),
        (Lease.Status.ODPOVEDANA, date(2026, 6, 30), date(2026, 3, 15), date(2026, 3, 15)),
        (Lease.Status.PRENEHALA, date(2026, 6, 30), date(2026, 3, 15), date(2026, 3, 15)),
        (Lease.Status.AKTIVNA, None, None, None),
    ],
)
def test_efektivna_veljavnost_pogodbe(status, datum_konca, datum_prenehanja, pricakovani_konec):
    pogodba = Lease(
        datum_zacetka=date(2026, 1, 10),
        datum_konca=datum_konca,
        datum_prenehanja=datum_prenehanja,
        status=status,
    )
    assert efektivna_veljavnost_pogodbe(pogodba) == (date(2026, 1, 10), pricakovani_konec)


def test_efektivna_veljavnost_postavke_je_omejena_s_pogodbo():
    item = postavka(
        datum_zacetka_postavke=date(2025, 12, 1),
        datum_prenehanja_postavke=date(2026, 3, 31),
    )
    item.lease.datum_zacetka = date(2026, 1, 10)
    item.lease.datum_konca = date(2026, 2, 15)
    assert efektivna_veljavnost_postavke(item) == (date(2026, 1, 10), date(2026, 2, 15))


@pytest.mark.parametrize(
    ("leto", "mesec", "zadnji_dan"),
    [(2025, 2, 28), (2024, 2, 29), (2026, 4, 30), (2026, 1, 31)],
)
def test_polni_mesec_obracuna_polno_osnovo(leto, mesec, zadnji_dan):
    item = postavka()
    item.lease.datum_zacetka = date(2024, 1, 1)
    rezultat = izracunaj_postavko(item, date(leto, mesec, 1), date(leto, mesec, zadnji_dan))
    assert rezultat.osnova == Decimal("310.00")
    assert rezultat.ddv_znesek == Decimal("68.20")
    assert rezultat.skupaj == Decimal("378.20")
    assert rezultat.je_sorazmerno is False


@pytest.mark.parametrize(
    ("sprememba", "datum", "osnova"),
    [
        ("lease_start", date(2026, 1, 16), "160.00"),
        ("lease_end", date(2026, 1, 15), "150.00"),
        ("item_start", date(2026, 1, 11), "210.00"),
        ("item_end", date(2026, 1, 20), "200.00"),
    ],
)
def test_del_meseca(sprememba, datum, osnova):
    item = postavka()
    if sprememba == "lease_start":
        item.lease.datum_zacetka = datum
    elif sprememba == "lease_end":
        item.lease.status = Lease.Status.PRENEHALA
        item.lease.datum_prenehanja = datum
    elif sprememba == "item_start":
        item.datum_zacetka_postavke = datum
    else:
        item.datum_prenehanja_postavke = datum
    rezultat = izracunaj_postavko(item, date(2026, 1, 1), date(2026, 1, 31))
    assert rezultat.osnova == Decimal(osnova)
    assert rezultat.je_sorazmerno is True


def test_postavka_zunaj_obdobja_in_arhiv_ne_gresta_v_nov_obracun():
    item = postavka(datum_zacetka_postavke=date(2026, 2, 1))
    assert izracunaj_postavko(item, date(2026, 1, 1), date(2026, 1, 31)) is None
    item.datum_zacetka_postavke = None
    item.arhivirana = True
    assert izracunaj_postavko(item, date(2026, 1, 1), date(2026, 1, 31)) is None
    item.arhivirana = False
    item.lease.arhivirana = True
    assert izracunaj_postavko(item, date(2026, 1, 1), date(2026, 1, 31)) is None


@pytest.mark.parametrize(
    ("nacin", "pricakovani_ddv"),
    [
        (VatRegime.NacinObracuna.OBDAVCENO, "68.20"),
        (VatRegime.NacinObracuna.OPROSCENO, "0.00"),
        (VatRegime.NacinObracuna.NI_PREDMET, "0.00"),
        (VatRegime.NacinObracuna.OBRNJENA_DAVCNA, "0.00"),
    ],
)
def test_ddv_in_posnetki_rezima(nacin, pricakovani_ddv):
    item = postavka()
    item.vat_regime.nacin_obracuna = nacin
    item.vat_regime.klavzula = "Klavzula"
    rezultat = izracunaj_postavko(item, date(2026, 1, 1), date(2026, 1, 31))
    assert rezultat.ddv_stopnja == Decimal("22.00")
    assert rezultat.ddv_znesek == Decimal(pricakovani_ddv)
    assert rezultat.skupaj == Decimal("310.00") + Decimal(pricakovani_ddv)
    assert rezultat.ddv_nacin_obracuna == nacin
    assert rezultat.ddv_klavzula == "Klavzula"
    assert rezultat.ddv_rekap_skupina == "DDV 22 %"
    item.vat_regime.klavzula = "Spremenjeno"
    assert rezultat.ddv_klavzula == "Klavzula"


def test_round_half_up_in_nicelni_sorazmerni_znesek():
    assert izracunaj_osnovo(Decimal("1.01"), 15, 30) == Decimal("0.51")
    assert izracunaj_osnovo(Decimal("1.00"), 0, 30) is None
    item = postavka(znesek_neto_mesecno=Decimal("0.01"), datum_zacetka_postavke=date(2026, 1, 31))
    rezultat = izracunaj_postavko(item, date(2026, 1, 1), date(2026, 1, 31))
    assert rezultat.osnova == Decimal("0.00")
    assert rezultat.je_sorazmerno is True


def test_ddv_se_zaokrozi_half_up():
    item = postavka(znesek_neto_mesecno=Decimal("1.00"))
    item.vat_regime.stopnja = Decimal("0.50")
    rezultat = izracunaj_postavko(item, date(2026, 1, 1), date(2026, 1, 31))
    assert rezultat.ddv_znesek == Decimal("0.01")

@pytest.mark.parametrize(
    ("zacetek", "konec", "osnova", "sorazmerno"),
    [
        (date(2026, 1, 1), date(2026, 1, 31), "310.00", False),
        (date(2026, 1, 10), date(2026, 1, 20), "110.00", True),
        (date(2026, 1, 31), date(2026, 1, 31), "10.00", True),
    ],
)
def test_meje_in_kratka_veljavnost_znotraj_meseca(zacetek, konec, osnova, sorazmerno):
    item = postavka(datum_zacetka_postavke=zacetek, datum_prenehanja_postavke=konec)
    rezultat = izracunaj_postavko(item, date(2026, 1, 1), date(2026, 1, 31))
    assert rezultat.osnova == Decimal(osnova)
    assert rezultat.je_sorazmerno is sorazmerno

@pytest.mark.parametrize(
    ("pot", "polje"),
    [
        (("lease", "unit", "property", "company"), "aktivno"),
        (("lease", "unit", "property"), "aktivna"),
        (("lease", "unit"), "aktivna"),
        (("lease", "tenant"), "aktiven"),
        (("lease",), "arhivirana"),
        ((), "arhivirana"),
    ],
)
def test_neaktiven_clen_verige_izkljuci_nov_obracun(pot, polje):
    item = postavka()
    clen = item
    for ime in pot:
        clen = getattr(clen, ime)
    setattr(clen, polje, not getattr(clen, polje))
    assert izracunaj_postavko(item, date(2026, 1, 1), date(2026, 1, 31)) is None
@pytest.mark.parametrize(
    ("obdobje_od", "obdobje_do", "zacetek", "konec", "mesecni_znesek", "osnova"),
    [
        (
            date(2025, 2, 1), date(2025, 2, 28),
            date(2025, 2, 15), date(2025, 2, 28),
            "280.00", "140.00",
        ),
        (
            date(2024, 2, 1), date(2024, 2, 29),
            date(2024, 2, 15), date(2024, 2, 29),
            "290.00", "150.00",
        ),
        (
            date(2026, 4, 1), date(2026, 4, 30),
            date(2026, 4, 11), date(2026, 4, 20),
            "300.00", "100.00",
        ),
    ],
)
def test_delni_meseci_uporabijo_dejansko_stevilo_dni(
    obdobje_od, obdobje_do, zacetek, konec, mesecni_znesek, osnova
):
    item = postavka(
        znesek_neto_mesecno=Decimal(mesecni_znesek),
        datum_zacetka_postavke=zacetek,
        datum_prenehanja_postavke=konec,
    )
    item.lease.datum_zacetka = date(2024, 1, 1)

    rezultat = izracunaj_postavko(item, obdobje_od, obdobje_do)

    assert rezultat.osnova == Decimal(osnova)
    assert rezultat.je_sorazmerno is True


def test_izracun_uposteva_presecisce_pogodbe_in_postavke():
    item = postavka(
        znesek_neto_mesecno=Decimal("300.00"),
        datum_zacetka_postavke=date(2026, 4, 5),
        datum_prenehanja_postavke=date(2026, 4, 20),
    )
    item.lease.datum_zacetka = date(2026, 4, 10)
    item.lease.datum_konca = date(2026, 4, 25)

    rezultat = izracunaj_postavko(item, date(2026, 4, 1), date(2026, 4, 30))

    assert rezultat.osnova == Decimal("110.00")
    assert rezultat.je_sorazmerno is True


def test_odpovedana_pogodba_uposteva_datum_prenehanja_v_izracunu():
    item = postavka(znesek_neto_mesecno=Decimal("300.00"))
    item.lease.status = Lease.Status.ODPOVEDANA
    item.lease.datum_konca = date(2026, 4, 30)
    item.lease.datum_prenehanja = date(2026, 4, 12)

    rezultat = izracunaj_postavko(item, date(2026, 4, 1), date(2026, 4, 30))

    assert rezultat.osnova == Decimal("120.00")
    assert rezultat.ddv_znesek == Decimal("26.40")
    assert rezultat.skupaj == Decimal("146.40")
    assert rezultat.je_sorazmerno is True