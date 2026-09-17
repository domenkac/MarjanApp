from decimal import Decimal

import pytest

from core.pravila import izracunaj_iz_bruto, slugify_naziv, zaokrozi


@pytest.mark.parametrize(
    ("vrednost", "pricakovano"),
    [
        ("1.005", "1.01"),
        ("-1.005", "-1.01"),
        ("2.675", "2.68"),
        ("-2.675", "-2.68"),
        ("12.34", "12.34"),
        ("-12.34", "-12.34"),
        ("0", "0.00"),
        ("12.344", "12.34"),
        ("-12.344", "-12.34"),
    ],
)
def test_zaokrozi_na_cent(vrednost, pricakovano):
    assert zaokrozi(Decimal(vrednost)) == Decimal(pricakovano)


@pytest.mark.parametrize(
    ("naziv", "pricakovano"),
    [
        ("Alfa d.o.o.", "Alfa-doo"),
        ("ČŠŽ čšž", "CSZ-csz"),
        ("Alfa nepremičnine d.o.o.", "Alfa-nepremicnine-doo"),
        ("  Alfa   d.o.o.  ", "Alfa-doo"),
        ("  Črta\tširina\nžarek  ", "Crta-sirina-zarek"),
        ("A..B", "AB"),
    ],
)
def test_slugify_naziv(naziv, pricakovano):
    assert slugify_naziv(naziv) == pricakovano


@pytest.mark.parametrize(
    ("bruto", "stopnja", "osnova", "ddv"),
    [
        ("122.00", "22", "100.00", "22.00"),
        ("0.01", "22", "0.01", "0.00"),
        ("0.03", "22", "0.02", "0.01"),
        ("-0.03", "22", "-0.02", "-0.01"),
    ],
)
def test_izracun_iz_bruto_ohrani_natancno_vsoto(bruto, stopnja, osnova, ddv):
    izracun = izracunaj_iz_bruto(Decimal(bruto), Decimal(stopnja))
    assert izracun == (Decimal(osnova), Decimal(ddv))
    assert sum(izracun) == Decimal(bruto)
