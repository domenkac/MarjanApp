import pytest

from core.eslog_konfiguracija import (
    ESLOG_KONFIGURACIJA_VERZIJA,
    POLJA_V1,
    PRIVZETI_SEGMENTI_PODJETJA_V1,
    SEGMENTI_V1,
    EslogKonfiguracijaNapaka,
    normaliziraj_neobvezna_polja,
    normaliziraj_segmentne_nastavitve,
    razresi_segmentne_nastavitve,
    validiraj_ucinkovito_konfiguracijo,
    zgodovinski_baseline_v1,
)

ELEKTRONSKI_NASLOV = "elektronski_naslov_kupca_vkljucen"
KONTAKTNI_PODATKI = "kontaktni_podatki_kupca_vkljuceni"


def test_register_v1_ima_fiksno_verzijo_in_samo_podprte_kljuce():
    assert ESLOG_KONFIGURACIJA_VERZIJA == "eslog-2.0-v200-config-v1"
    assert set(SEGMENTI_V1) == {ELEKTRONSKI_NASLOV, KONTAKTNI_PODATKI}
    assert set(POLJA_V1) == {
        "elektronski_naslov_kupca",
        "kontaktna_tocka_kupca",
        "kontaktni_telefon_kupca",
        "kontaktni_email_kupca",
    }
    assert "referencna_oznaka_kupca" not in POLJA_V1
    assert "referencna_oznaka_racunovodskega_konta_kupca" not in POLJA_V1


def test_register_v1_zamrzne_uradne_preslikave_segmentov():
    elektronski_naslov = SEGMENTI_V1[ELEKTRONSKI_NASLOV]
    assert (
        elektronski_naslov.poslovni_termin,
        elektronski_naslov.party_kvalifikator,
        elektronski_naslov.skupina,
        elektronski_naslov.cta_kvalifikator,
    ) == ("BT-49/BT-49-1", "BY", "G_SG5", "IC")

    kontaktni_podatki = SEGMENTI_V1[KONTAKTNI_PODATKI]
    assert (
        kontaktni_podatki.poslovni_termin,
        kontaktni_podatki.party_kvalifikator,
        kontaktni_podatki.skupina,
        kontaktni_podatki.cta_kvalifikator,
    ) == ("BG-9", "BY", "G_SG5", "PD")


def test_register_v1_zamrzne_uradne_preslikave_polj():
    pricakovano = {
        "elektronski_naslov_kupca": (
            "BT-49", ELEKTRONSKI_NASLOV, 512, "email", "D_3148", "EM"
        ),
        "kontaktna_tocka_kupca": (
            "BT-56", KONTAKTNI_PODATKI, 256, "besedilo", "D_3412", None
        ),
        "kontaktni_telefon_kupca": (
            "BT-57", KONTAKTNI_PODATKI, 512, "besedilo", "D_3148", "TE"
        ),
        "kontaktni_email_kupca": (
            "BT-58", KONTAKTNI_PODATKI, 512, "email", "D_3148", "EM"
        ),
    }
    for kljuc, definicija in POLJA_V1.items():
        assert (
            definicija.poslovni_termin,
            definicija.segment_kljuc,
            definicija.najvec_znakov,
            definicija.vrsta,
            definicija.xml_element,
            definicija.com_kvalifikator,
        ) == pricakovano[kljuc]


def test_privzeti_segmenti_podjetja_so_izrecno_false():
    assert dict(PRIVZETI_SEGMENTI_PODJETJA_V1) == {
        ELEKTRONSKI_NASLOV: False,
        KONTAKTNI_PODATKI: False,
    }


@pytest.mark.parametrize(
    ("podatki", "koda"),
    [
        ({"neznan_segment": True}, "ESLOG_SEGMENT_KLJUC_NEVELJAVEN"),
        ({ELEKTRONSKI_NASLOV: 1}, "ESLOG_SEGMENT_VREDNOST_NEVELJAVNA"),
        ({ELEKTRONSKI_NASLOV: 0}, "ESLOG_SEGMENT_VREDNOST_NEVELJAVNA"),
        ({ELEKTRONSKI_NASLOV: None}, "ESLOG_SEGMENT_VREDNOST_NEVELJAVNA"),
    ],
)
def test_segmentne_nastavitve_zavrnejo_neveljaven_vhod(podatki, koda):
    with pytest.raises(EslogKonfiguracijaNapaka) as napaka:
        normaliziraj_segmentne_nastavitve(podatki)
    assert napaka.value.koda == koda


def test_tenant_delni_override_ne_potrebuje_null_vrednosti():
    rezultat = razresi_segmentne_nastavitve(
        PRIVZETI_SEGMENTI_PODJETJA_V1,
        {ELEKTRONSKI_NASLOV: True},
    )
    assert rezultat == {
        ELEKTRONSKI_NASLOV: True,
        KONTAKTNI_PODATKI: False,
    }


def test_tenant_false_izrecno_povozi_company_true():
    rezultat = razresi_segmentne_nastavitve(
        {
            ELEKTRONSKI_NASLOV: True,
            KONTAKTNI_PODATKI: False,
        },
        {ELEKTRONSKI_NASLOV: False},
    )
    assert rezultat == {
        ELEKTRONSKI_NASLOV: False,
        KONTAKTNI_PODATKI: False,
    }


@pytest.mark.parametrize(
    "kljuc",
    [
        "referencna_oznaka_kupca",
        "referencna_oznaka_racunovodskega_konta_kupca",
        "poljuben_xml_element",
    ],
)
def test_nepodprto_ali_neznano_polje_je_zavrnjeno(kljuc):
    with pytest.raises(EslogKonfiguracijaNapaka) as napaka:
        normaliziraj_neobvezna_polja({kljuc: "vrednost"})
    assert napaka.value.koda == "ESLOG_POLJE_KLJUC_NEVELJAVEN"


@pytest.mark.parametrize(
    "polja",
    [
        {"elektronski_naslov_kupca": "ni-email"},
        {"kontaktni_email_kupca": "ni-email"},
        {"elektronski_naslov_kupca": "a" * 503 + "@x.example"},
        {"kontaktna_tocka_kupca": "x" * 257},
        {"kontaktni_telefon_kupca": "x" * 513},
        {"kontaktni_email_kupca": "a" * 503 + "@x.example"},
    ],
)
def test_neveljavne_in_predolge_vrednosti_so_zavrnjene(polja):
    with pytest.raises(EslogKonfiguracijaNapaka):
        normaliziraj_neobvezna_polja(polja)


def test_normalizacija_je_deterministicna_in_sledi_vrstnemu_redu_registra():
    vhod = {
        "kontaktni_email_kupca": "  racuni@example.com  ",
        "kontaktna_tocka_kupca": "  Nabava  ",
    }
    prvi = normaliziraj_neobvezna_polja(vhod)
    drugi = normaliziraj_neobvezna_polja(dict(reversed(tuple(vhod.items()))))
    assert prvi == drugi == {
        "kontaktna_tocka_kupca": "Nabava",
        "kontaktni_email_kupca": "racuni@example.com",
    }
    assert tuple(prvi) == ("kontaktna_tocka_kupca", "kontaktni_email_kupca")


@pytest.mark.parametrize(
    ("segmenti", "polja", "koda"),
    [
        (
            {ELEKTRONSKI_NASLOV: True, KONTAKTNI_PODATKI: False},
            {},
            "ESLOG_ELEKTRONSKI_NASLOV_MANJKA",
        ),
        (
            {ELEKTRONSKI_NASLOV: False, KONTAKTNI_PODATKI: False},
            {"elektronski_naslov_kupca": "racuni@example.com"},
            "ESLOG_ELEKTRONSKI_NASLOV_NI_DOVOLJEN",
        ),
        (
            {ELEKTRONSKI_NASLOV: False, KONTAKTNI_PODATKI: True},
            {},
            "ESLOG_KONTAKTNI_PODATKI_MANJKAJO",
        ),
        (
            {ELEKTRONSKI_NASLOV: False, KONTAKTNI_PODATKI: False},
            {"kontaktni_telefon_kupca": "+386 1 234 56 78"},
            "ESLOG_KONTAKTNI_PODATKI_NISO_DOVOLJENI",
        ),
    ],
)
def test_odvisnosti_segmentov_in_polj_fail_closed(segmenti, polja, koda):
    with pytest.raises(EslogKonfiguracijaNapaka) as napaka:
        validiraj_ucinkovito_konfiguracijo(segmenti, polja)
    assert napaka.value.koda == koda


def test_veljavna_ucinkovita_konfiguracija_je_normalizirana():
    assert validiraj_ucinkovito_konfiguracijo(
        {ELEKTRONSKI_NASLOV: True, KONTAKTNI_PODATKI: True},
        {
            "elektronski_naslov_kupca": " racuni@example.com ",
            "kontaktni_telefon_kupca": " +386 1 234 56 78 ",
        },
    ) == {
        "version": ESLOG_KONFIGURACIJA_VERZIJA,
        "segments": {
            ELEKTRONSKI_NASLOV: True,
            KONTAKTNI_PODATKI: True,
        },
        "fields": {
            "elektronski_naslov_kupca": "racuni@example.com",
            "kontaktni_telefon_kupca": "+386 1 234 56 78",
        },
    }


def test_poljubnega_kljuca_ni_mogoce_pretvoriti_v_xml_target():
    with pytest.raises(EslogKonfiguracijaNapaka):
        normaliziraj_neobvezna_polja({"D_3148": "napad@example.com"})
    assert {
        definicija.xml_element for definicija in POLJA_V1.values()
    } == {"D_3148", "D_3412"}


def test_zgodovinski_baseline_v1_ima_zamrznjeno_eksplicitno_stanje():
    assert zgodovinski_baseline_v1() == {
        "version": "eslog-2.0-v200-config-v1",
        "segments": {
            ELEKTRONSKI_NASLOV: False,
            KONTAKTNI_PODATKI: False,
        },
        "fields": {},
    }
