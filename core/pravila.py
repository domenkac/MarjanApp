import re
from decimal import ROUND_HALF_UP, Decimal

_SUMNIKI = str.maketrans("čšžČŠŽ", "cszCSZ")


def zaokrozi(vrednost: Decimal) -> Decimal:
    """Aritmetično zaokroževanje na cent. Samo za denarne vrednosti."""
    return vrednost.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def izracunaj_iz_bruto(bruto: Decimal, stopnja: Decimal) -> tuple[Decimal, Decimal]:
    osnova = zaokrozi(bruto / (1 + stopnja / Decimal(100)))
    return osnova, bruto - osnova


def slugify_naziv(naziv: str) -> str:
    """Company.naziv spremeni v del imena datoteke brez šumnikov in pik."""
    brez_sumnikov = naziv.translate(_SUMNIKI)
    brez_pik = brez_sumnikov.replace(".", "")
    return re.sub(r"\s+", "-", brez_pik.strip())
