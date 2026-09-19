from pathlib import Path

from django.conf import settings


class NeveljavnaRenderPot(ValueError):
    pass


def varna_render_pot(ime_datoteke: str) -> Path:
    if (
        not ime_datoteke
        or Path(ime_datoteke).is_absolute()
        or any(znak in ime_datoteke for znak in ("/", chr(92), ".."))
        or chr(0) in ime_datoteke
    ):
        raise NeveljavnaRenderPot("Neveljavno ime render datoteke.")

    koren = Path(settings.RENDERING_ROOT).resolve()
    cilj = (koren / ime_datoteke).resolve()
    try:
        cilj.relative_to(koren)
    except ValueError as napaka:
        raise NeveljavnaRenderPot("Render pot je zunaj dovoljene mape.") from napaka
    return cilj