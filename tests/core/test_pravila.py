from decimal import Decimal

import pytest

from core.pravila import slugify_naziv, zaokrozi


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
