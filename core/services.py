from dataclasses import dataclass


@dataclass(frozen=True)
class StanjeRazlog:
    koda: str
    sporocilo: str


@dataclass(frozen=True)
class StanjeRezultat:
    uspesno: bool
    razlogi: tuple[StanjeRazlog, ...] = ()


@dataclass(frozen=True)
class IzbrisRazlog:
    koda: str
    sporocilo: str
    tip_povezave: str
    stevilo_povezav: int


@dataclass(frozen=True)
class IzbrisOdlocitev:
    dovoljeno: bool
    razlogi: tuple[IzbrisRazlog, ...] = ()
    mozno_arhiviranje: bool = False
