from dataclasses import dataclass


@dataclass(frozen=True)
class StanjeRazlog:
    koda: str
    sporocilo: str


@dataclass(frozen=True)
class StanjeRezultat:
    uspesno: bool
    razlogi: tuple[StanjeRazlog, ...] = ()
