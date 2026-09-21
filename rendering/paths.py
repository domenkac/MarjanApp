from pathlib import Path

from django.conf import settings


class NeveljavnaRenderPot(ValueError):
    pass


def _preveri_ime_datoteke(ime_datoteke: str) -> None:
    if (
        not ime_datoteke
        or Path(ime_datoteke).is_absolute()
        or any(znak in ime_datoteke for znak in ("/", chr(92), ".."))
        or chr(0) in ime_datoteke
    ):
        raise NeveljavnaRenderPot("Neveljavno ime render datoteke.")


def _preveri_cilj(koren: Path, cilj: Path) -> None:
    try:
        cilj.relative_to(koren)
    except ValueError as napaka:
        raise NeveljavnaRenderPot("Render pot je zunaj dovoljene mape.") from napaka


def varna_render_pot(ime_datoteke: str) -> Path:
    _preveri_ime_datoteke(ime_datoteke)

    koren = Path(settings.RENDERING_ROOT).resolve()
    cilj = (koren / ime_datoteke).resolve()
    _preveri_cilj(koren, cilj)
    return cilj


def varna_render_pot_podjetja(company_id: int, ime_datoteke: str) -> tuple[str, Path]:
    if isinstance(company_id, bool) or not isinstance(company_id, int) or company_id < 1:
        raise NeveljavnaRenderPot("Neveljaven ID podjetja za render pot.")
    _preveri_ime_datoteke(ime_datoteke)

    relativna_pot = Path(f"company-{company_id}") / ime_datoteke
    koren = Path(settings.RENDERING_ROOT).resolve()
    cilj = (koren / relativna_pot).resolve()
    _preveri_cilj(koren, cilj)
    return relativna_pot.as_posix(), cilj
