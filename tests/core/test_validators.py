import pytest

from core.validators import preveri_davcno_stevilko, preveri_maticno_stevilko


@pytest.mark.parametrize(
    ("stevilka", "veljavna"),
    [
        ("15012557", True),
        ("70000000", True),
        ("15012558", False),
        ("00000000", False),
        ("1501255", False),
        ("1501255A", False),
    ],
)
def test_davcna_stevilka(stevilka, veljavna):
    assert preveri_davcno_stevilko(stevilka) is veljavna


@pytest.mark.parametrize(
    ("stevilka", "veljavna"),
    [
        ("1234567000", True),
        ("123456789", False),
        ("12345678901", False),
        ("123456789A", False),
    ],
)
def test_maticna_stevilka(stevilka, veljavna):
    assert preveri_maticno_stevilko(stevilka) is veljavna
