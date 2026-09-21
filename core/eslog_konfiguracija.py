import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

ESLOG_KONFIGURACIJA_VERZIJA = "eslog-2.0-v200-config-v1"

ELEKTRONSKI_NASLOV_KUPCA_VKLJUCEN = "elektronski_naslov_kupca_vkljucen"
KONTAKTNI_PODATKI_KUPCA_VKLJUCENI = "kontaktni_podatki_kupca_vkljuceni"

ELEKTRONSKI_NASLOV_KUPCA = "elektronski_naslov_kupca"
KONTAKTNA_TOCKA_KUPCA = "kontaktna_tocka_kupca"
KONTAKTNI_TELEFON_KUPCA = "kontaktni_telefon_kupca"
KONTAKTNI_EMAIL_KUPCA = "kontaktni_email_kupca"


class EslogKonfiguracijaNapaka(ValueError):
    def __init__(self, koda: str, sporocilo: str):
        self.koda = koda
        super().__init__(sporocilo)


@dataclass(frozen=True, slots=True)
class SegmentDefinicija:
    poslovni_termin: str
    party_kvalifikator: str
    skupina: str
    cta_kvalifikator: str


@dataclass(frozen=True, slots=True)
class PoljeDefinicija:
    poslovni_termin: str
    segment_kljuc: str
    najvec_znakov: int
    vrsta: str
    xml_element: str
    com_kvalifikator: str | None = None


SEGMENTI_V1 = MappingProxyType(
    {
        ELEKTRONSKI_NASLOV_KUPCA_VKLJUCEN: SegmentDefinicija(
            poslovni_termin="BT-49/BT-49-1",
            party_kvalifikator="BY",
            skupina="G_SG5",
            cta_kvalifikator="IC",
        ),
        KONTAKTNI_PODATKI_KUPCA_VKLJUCENI: SegmentDefinicija(
            poslovni_termin="BG-9",
            party_kvalifikator="BY",
            skupina="G_SG5",
            cta_kvalifikator="PD",
        ),
    }
)

POLJA_V1 = MappingProxyType(
    {
        ELEKTRONSKI_NASLOV_KUPCA: PoljeDefinicija(
            poslovni_termin="BT-49",
            segment_kljuc=ELEKTRONSKI_NASLOV_KUPCA_VKLJUCEN,
            najvec_znakov=512,
            vrsta="email",
            xml_element="D_3148",
            com_kvalifikator="EM",
        ),
        KONTAKTNA_TOCKA_KUPCA: PoljeDefinicija(
            poslovni_termin="BT-56",
            segment_kljuc=KONTAKTNI_PODATKI_KUPCA_VKLJUCENI,
            najvec_znakov=256,
            vrsta="besedilo",
            xml_element="D_3412",
        ),
        KONTAKTNI_TELEFON_KUPCA: PoljeDefinicija(
            poslovni_termin="BT-57",
            segment_kljuc=KONTAKTNI_PODATKI_KUPCA_VKLJUCENI,
            najvec_znakov=512,
            vrsta="besedilo",
            xml_element="D_3148",
            com_kvalifikator="TE",
        ),
        KONTAKTNI_EMAIL_KUPCA: PoljeDefinicija(
            poslovni_termin="BT-58",
            segment_kljuc=KONTAKTNI_PODATKI_KUPCA_VKLJUCENI,
            najvec_znakov=512,
            vrsta="email",
            xml_element="D_3148",
            com_kvalifikator="EM",
        ),
    }
)

PRIVZETI_SEGMENTI_PODJETJA_V1 = MappingProxyType(
    {
        ELEKTRONSKI_NASLOV_KUPCA_VKLJUCEN: False,
        KONTAKTNI_PODATKI_KUPCA_VKLJUCENI: False,
    }
)


def privzeti_segmenti_podjetja_v1() -> dict[str, bool]:
    """Vrne nov, trajno zamrznjen Company default za konfiguracijo v1."""
    return {
        "elektronski_naslov_kupca_vkljucen": False,
        "kontaktni_podatki_kupca_vkljuceni": False,
    }

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+$")
_KONTAKTNA_POLJA = frozenset(
    {KONTAKTNA_TOCKA_KUPCA, KONTAKTNI_TELEFON_KUPCA, KONTAKTNI_EMAIL_KUPCA}
)


def _preveri_preslikavo(podatki: Any) -> Mapping[str, Any]:
    if not isinstance(podatki, Mapping):
        raise EslogKonfiguracijaNapaka(
            "ESLOG_KONFIGURACIJA_NEVELJAVNA",
            "eSLOG konfiguracija mora biti preslikava.",
        )
    return podatki


def normaliziraj_segmentne_nastavitve(podatki: Mapping[str, Any]) -> dict[str, bool]:
    podatki = _preveri_preslikavo(podatki)
    neznani = set(podatki) - set(SEGMENTI_V1)
    if neznani:
        raise EslogKonfiguracijaNapaka(
            "ESLOG_SEGMENT_KLJUC_NEVELJAVEN",
            f"Nepodprt ključ eSLOG segmenta: {min(neznani)}.",
        )
    for kljuc, vrednost in podatki.items():
        if type(vrednost) is not bool:
            raise EslogKonfiguracijaNapaka(
                "ESLOG_SEGMENT_VREDNOST_NEVELJAVNA",
                f"Vrednost segmenta {kljuc} mora biti True ali False.",
            )
    return {kljuc: podatki[kljuc] for kljuc in SEGMENTI_V1 if kljuc in podatki}


def razresi_segmentne_nastavitve(
    privzete: Mapping[str, Any], override: Mapping[str, Any]
) -> dict[str, bool]:
    privzete = normaliziraj_segmentne_nastavitve(privzete)
    override = normaliziraj_segmentne_nastavitve(override)
    if set(privzete) != set(SEGMENTI_V1):
        raise EslogKonfiguracijaNapaka(
            "ESLOG_PRIVZETI_SEGMENTI_MANJKAJO",
            "Company mora določiti privzeto vrednost vseh podprtih segmentov.",
        )
    return {kljuc: override.get(kljuc, privzete[kljuc]) for kljuc in SEGMENTI_V1}


def normaliziraj_neobvezna_polja(podatki: Mapping[str, Any]) -> dict[str, str]:
    podatki = _preveri_preslikavo(podatki)
    neznani = set(podatki) - set(POLJA_V1)
    if neznani:
        raise EslogKonfiguracijaNapaka(
            "ESLOG_POLJE_KLJUC_NEVELJAVEN",
            f"Nepodprt ključ eSLOG polja: {min(neznani)}.",
        )

    rezultat = {}
    for kljuc, definicija in POLJA_V1.items():
        if kljuc not in podatki:
            continue
        vrednost = podatki[kljuc]
        if not isinstance(vrednost, str) or not (vrednost := vrednost.strip()):
            raise EslogKonfiguracijaNapaka(
                "ESLOG_POLJE_VREDNOST_NEVELJAVNA",
                f"Vrednost polja {kljuc} mora biti neprazen niz.",
            )
        if len(vrednost) > definicija.najvec_znakov:
            raise EslogKonfiguracijaNapaka(
                "ESLOG_POLJE_PREDOLGO",
                f"Vrednost polja {kljuc} je predolga.",
            )
        if definicija.vrsta == "email" and _EMAIL.fullmatch(vrednost) is None:
            raise EslogKonfiguracijaNapaka(
                "ESLOG_EMAIL_NEVELJAVEN",
                f"Vrednost polja {kljuc} ni veljaven elektronski naslov.",
            )
        rezultat[kljuc] = vrednost
    return rezultat


def validiraj_ucinkovito_konfiguracijo(
    segmenti: Mapping[str, Any], polja: Mapping[str, Any]
) -> dict[str, Any]:
    segmenti = normaliziraj_segmentne_nastavitve(segmenti)
    if set(segmenti) != set(SEGMENTI_V1):
        raise EslogKonfiguracijaNapaka(
            "ESLOG_UCINKOVITI_SEGMENTI_MANJKAJO",
            "Učinkovita konfiguracija mora določiti vse podprte segmente.",
        )
    polja = normaliziraj_neobvezna_polja(polja)

    elektronski_naslov = ELEKTRONSKI_NASLOV_KUPCA in polja
    if segmenti[ELEKTRONSKI_NASLOV_KUPCA_VKLJUCEN] and not elektronski_naslov:
        raise EslogKonfiguracijaNapaka(
            "ESLOG_ELEKTRONSKI_NASLOV_MANJKA",
            "Vključen segment elektronskega naslova kupca zahteva BT-49.",
        )
    if not segmenti[ELEKTRONSKI_NASLOV_KUPCA_VKLJUCEN] and elektronski_naslov:
        raise EslogKonfiguracijaNapaka(
            "ESLOG_ELEKTRONSKI_NASLOV_NI_DOVOLJEN",
            "BT-49 ni dovoljen, ko je segment elektronskega naslova izključen.",
        )

    kontaktna_polja = _KONTAKTNA_POLJA.intersection(polja)
    if segmenti[KONTAKTNI_PODATKI_KUPCA_VKLJUCENI] and not kontaktna_polja:
        raise EslogKonfiguracijaNapaka(
            "ESLOG_KONTAKTNI_PODATKI_MANJKAJO",
            "Vključen segment kontaktnih podatkov kupca zahteva BT-56, BT-57 ali BT-58.",
        )
    if not segmenti[KONTAKTNI_PODATKI_KUPCA_VKLJUCENI] and kontaktna_polja:
        raise EslogKonfiguracijaNapaka(
            "ESLOG_KONTAKTNI_PODATKI_NISO_DOVOLJENI",
            "Kontaktna polja niso dovoljena, ko je segment kontaktnih podatkov izključen.",
        )

    return {
        "version": ESLOG_KONFIGURACIJA_VERZIJA,
        "segments": dict(segmenti),
        "fields": polja,
    }


def validiraj_eslog_konfiguracija_snapshot(snapshot: Any) -> dict[str, Any]:
    snapshot = _preveri_preslikavo(snapshot)
    if set(snapshot) != {"version", "segments", "fields"}:
        raise EslogKonfiguracijaNapaka(
            "ESLOG_SNAPSHOT_STRUKTURA_NEVELJAVNA",
            "eSLOG konfiguracijski snapshot mora vsebovati samo version, segments in fields.",
        )
    if snapshot["version"] != ESLOG_KONFIGURACIJA_VERZIJA:
        raise EslogKonfiguracijaNapaka(
            "ESLOG_SNAPSHOT_VERZIJA_NEVELJAVNA",
            "Različica eSLOG konfiguracijskega snapshota ni podprta.",
        )
    return validiraj_ucinkovito_konfiguracijo(
        snapshot["segments"], snapshot["fields"]
    )


def razresi_eslog_konfiguracijo(
    company_defaults: Mapping[str, Any],
    tenant_overrides: Mapping[str, Any],
    tenant_fields: Mapping[str, Any],
) -> dict[str, Any]:
    return validiraj_ucinkovito_konfiguracijo(
        razresi_segmentne_nastavitve(company_defaults, tenant_overrides),
        tenant_fields,
    )


def zgodovinski_baseline_v1() -> dict[str, Any]:
    return validiraj_ucinkovito_konfiguracijo(
        PRIVZETI_SEGMENTI_PODJETJA_V1,
        {},
    )
