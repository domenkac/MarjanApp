from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.db import transaction

from companies.models import NumberSequence, Signatory
from invoicing import services as invoicing_services
from invoicing.models import Invoice, InvoiceLine
from invoicing.services import (
    dodaj_vrstico,
    izbrisi_racun,
    izbrisi_vrstico,
    potrdi_korekcijski_osnutek,
    potrdi_osnutek_np,
    pripravi_korekcijski_osnutek,
    pripravi_osnutek_np,
    spremeni_podatke_racuna,
    spremeni_vrstico,
)
from tests.financial_fixtures import save_financial_fixture
from tests.image_data import slika_bytes
from tests.invoicing.test_priprava_np import dodaj_postavko
from tests.setup_helpers import approve_sample, prepare_initial_setup

pytest_plugins = ["tests.invoicing.test_priprava_np"]


def izdan_np(pogodba):
    dodaj_postavko(pogodba, znesek="100.00")
    dodaj_postavko(pogodba, naziv="Parkirišče", znesek="30.00", nacin="OPROSCENO")
    osnutek = pripravi_osnutek_np(
        pogodba, date(2026, 8, 1), date(2026, 8, 31), date(2026, 9, 1)
    ).invoice
    prepare_initial_setup(osnutek.company, osnutek.lease)
    return potrdi_osnutek_np(osnutek)


@pytest.mark.parametrize(
    "tip", [Invoice.DocumentType.STORNO, Invoice.DocumentType.DOBROPIS]
)
def test_razlog_je_obvezen_in_neizdan_original_zavrnjen(pogodba, tip):
    izdan = izdan_np(pogodba)
    with pytest.raises(ValidationError):
        pripravi_korekcijski_osnutek(izdan, tip, "  ", date(2026, 9, 2))
    assert Invoice.objects.count() == 1

    neizdan = pripravi_osnutek_np(
        pogodba, date(2026, 10, 1), date(2026, 10, 31), date(2026, 11, 1)
    ).invoice
    with pytest.raises(ValueError, match="izdan"):
        pripravi_korekcijski_osnutek(neizdan, tip, "Popravek", date(2026, 11, 2))
    assert Invoice.objects.count() == 2


def test_storno_zrcali_vrstice_in_ohrani_original(pogodba):
    original = izdan_np(pogodba)
    originalne = list(InvoiceLine.objects.filter(invoice=original).order_by("pk"))
    prvotne_vrstice = list(
        InvoiceLine.objects.filter(invoice=original).order_by("pk").values()
    )
    snapshot = (
        original.status,
        original.stevilka,
        original.skupaj_z_ddv,
        original.prejemnik_naziv,
        original.izdajatelj_naziv,
    )

    storno = pripravi_korekcijski_osnutek(
        original, Invoice.DocumentType.STORNO, "Napaka", date(2026, 9, 2)
    )
    vrstice = list(InvoiceLine.objects.filter(invoice=storno).order_by("pk"))
    assert storno.povezan_racun_id == original.pk
    assert storno.company_id == original.company_id
    assert storno.lease_id == original.lease_id
    assert (storno.prejemnik_naziv, storno.izdajatelj_naziv) == (
        original.prejemnik_naziv,
        original.izdajatelj_naziv,
    )
    assert (storno.stevilka, storno.status) == (None, Invoice.Status.OSNUTEK)
    assert len(vrstice) == len(originalne)
    for nova, stara in zip(vrstice, originalne, strict=True):
        assert (
            nova.opis,
            nova.ddv_stopnja,
            nova.ddv_nacin_obracuna,
            nova.ddv_klavzula,
            nova.ddv_rekap_skupina,
        ) == (
            stara.opis,
            stara.ddv_stopnja,
            stara.ddv_nacin_obracuna,
            stara.ddv_klavzula,
            stara.ddv_rekap_skupina,
        )
        assert (nova.osnova, nova.ddv_znesek, nova.skupaj) == (
            -stara.osnova,
            -stara.ddv_znesek,
            -stara.skupaj,
        )

    potrdi_korekcijski_osnutek(storno)
    original.refresh_from_db()
    assert (
        original.status,
        original.stevilka,
        original.skupaj_z_ddv,
        original.prejemnik_naziv,
        original.izdajatelj_naziv,
    ) == snapshot
    assert (
        list(InvoiceLine.objects.filter(invoice=original).order_by("pk").values())
        == prvotne_vrstice
    )


def test_storno_ohrani_eslog_snapshote_po_spremembi_zivih_podatkov(pogodba):
    original = izdan_np(pogodba)
    pricakovane_kode = list(
        InvoiceLine.objects.filter(invoice=original)
        .order_by("pk")
        .values_list("ddv_eslog_koda", flat=True)
    )
    storno = pripravi_korekcijski_osnutek(
        original, Invoice.DocumentType.STORNO, "Napaka", date(2026, 9, 2)
    )

    pogodba.tenant.tip = pogodba.tenant.Tip.FIZICNA_OSEBA
    pogodba.tenant.save(update_fields=["tip"])
    approve_sample(original.company, pogodba)
    for postavka in pogodba.leaseitem_set.select_related("vat_regime"):
        postavka.vat_regime.eslog_koda = "SPREMENJENA"
        save_financial_fixture(postavka.vat_regime, update_fields=["eslog_koda"])

    potrjen = potrdi_korekcijski_osnutek(storno)
    assert potrjen.prejemnik_tip == original.prejemnik_tip
    assert list(
        InvoiceLine.objects.filter(invoice=potrjen)
        .order_by("pk")
        .values_list("ddv_eslog_koda", flat=True)
    ) == pricakovane_kode


@pytest.mark.parametrize(
    "tip", [Invoice.DocumentType.STORNO, Invoice.DocumentType.DOBROPIS]
)
def test_korekcija_uporabi_podpisnika_veljavnega_na_svoj_datum(
    pogodba, tmp_path, tip
):
    original = izdan_np(pogodba)
    podpisnik_a = Signatory.objects.get(company=original.company)
    podpisnik_a.velja_do = date(2026, 9, 1)
    podpisnik_a.save(update_fields=["velja_do"])
    (tmp_path / "podpis-b.png").write_bytes(slika_bytes(size=(3, 3)))
    Signatory.objects.create(
        company=original.company,
        ime_priimek="Boris Podpisnik",
        funkcija="Direktor",
        podpis_slika="podpis-b.png",
        velja_od=date(2026, 9, 2),
    )
    popravek = pripravi_korekcijski_osnutek(
        original, tip, "Napaka", date(2026, 9, 2)
    )
    if tip == Invoice.DocumentType.DOBROPIS:
        dodaj_vrstico(
            popravek,
            opis="Dobropis",
            osnova=Decimal("-10.00"),
            ddv_stopnja=Decimal("0.00"),
            ddv_znesek=Decimal("0.00"),
            skupaj=Decimal("-10.00"),
            ddv_nacin_obracuna="NI_PREDMET",
            ddv_eslog_koda="O",
            ddv_rekap_skupina="NI_PREDMET",
        )

    approve_sample(original.company, pogodba)
    potrjen = potrdi_korekcijski_osnutek(popravek)
    original.refresh_from_db()

    assert potrjen.izdajatelj_podpisnik_ime == "Boris Podpisnik"
    assert potrjen.izdajatelj_podpisnik_funkcija == "Direktor"
    assert potrjen.izdajatelj_podpisnik_slika_pot.startswith(
        "nespremenljivi-asseti/"
    )
    assert original.izdajatelj_podpisnik_ime == "Ana Podpisnica"
    assert original.izdajatelj_podpisnik_funkcija == "Direktorica"
    assert original.izdajatelj_podpisnik_slika_pot != (
        potrjen.izdajatelj_podpisnik_slika_pot
    )


@pytest.mark.parametrize("napaka", ["manjka", "dvoumno"])
def test_neveljaven_podpisnik_korekcije_ne_porabi_stevilke(
    pogodba, tmp_path, napaka
):
    original = izdan_np(pogodba)
    storno = pripravi_korekcijski_osnutek(
        original, Invoice.DocumentType.STORNO, "Napaka", date(2026, 9, 2)
    )
    podpisnik = Signatory.objects.get(company=original.company)
    if napaka == "manjka":
        podpisnik.velja_do = date(2026, 9, 1)
        podpisnik.save(update_fields=["velja_do"])
    else:
        (tmp_path / "podpis-b.png").write_bytes(b"podpis-b")
        Signatory.objects.create(
            company=original.company,
            ime_priimek="Boris Podpisnik",
            funkcija="Direktor",
            podpis_slika="podpis-b.png",
            velja_od=date(2026, 9, 2),
            velja_do=date(2026, 9, 30),
        )

    with pytest.raises(ValueError, match="veljavnega podpisnika|veljavnih podpisnikov"):
        potrdi_korekcijski_osnutek(storno)

    storno.refresh_from_db()
    assert storno.stevilka is None
    assert NumberSequence.objects.get(
        company=original.company, tip=NumberSequence.Tip.NP, leto=0
    ).zadnja_stevilka == 1


def test_korekcija_nespremenljive_assete_pripravi_zunaj_transakcije(
    pogodba, monkeypatch
):
    original = izdan_np(pogodba)
    storno = pripravi_korekcijski_osnutek(
        original, Invoice.DocumentType.STORNO, "Napaka", date(2026, 9, 2)
    )
    shrani = invoicing_services.shrani_nespremenljiv_asset
    globina = len(transaction.get_connection().atomic_blocks)

    def preveri(pot):
        assert len(transaction.get_connection().atomic_blocks) == globina
        return shrani(pot)

    monkeypatch.setattr(invoicing_services, "shrani_nespremenljiv_asset", preveri)

    assert potrdi_korekcijski_osnutek(storno).stevilka == "NP-00002"


def test_dobropis_uporabi_rocno_vrstico_brez_izracuna_iz_pogodbe(pogodba):
    original = izdan_np(pogodba)
    dobropis = pripravi_korekcijski_osnutek(
        original, Invoice.DocumentType.DOBROPIS, "Popust", date(2026, 9, 2)
    )
    assert dobropis.povezan_racun_id == original.pk
    assert dobropis.stevilka is None
    assert not InvoiceLine.objects.filter(invoice=dobropis).exists()

    vrstica = dodaj_vrstico(
        dobropis,
        opis="Ročni dobropis",
        osnova=Decimal("-10.00"),
        ddv_stopnja=Decimal("22.00"),
        ddv_znesek=Decimal("-2.20"),
        skupaj=Decimal("-12.20"),
        ddv_nacin_obracuna="OBDAVCENO",
        ddv_eslog_koda="S",
        ddv_rekap_skupina="OBDAVCENO",
    )
    potrjen = potrdi_korekcijski_osnutek(dobropis)
    assert (potrjen.osnova_skupaj, potrjen.ddv_skupaj, potrjen.skupaj_z_ddv) == (
        Decimal("-10.00"),
        Decimal("-2.20"),
        Decimal("-12.20"),
    )
    assert vrstica.lease_item_id is None
    assert (
        vrstica.osnova != -InvoiceLine.objects.filter(invoice=original).first().osnova
    )


@pytest.mark.parametrize("koda", ["", "E"])
def test_dobropis_zavrne_manjkajoco_ali_napacno_eslog_kodo(pogodba, koda):
    original = izdan_np(pogodba)
    dobropis = pripravi_korekcijski_osnutek(
        original, Invoice.DocumentType.DOBROPIS, "Popust", date(2026, 9, 2)
    )
    dodaj_vrstico(
        dobropis,
        opis="Ročni dobropis",
        osnova=Decimal("-10.00"),
        ddv_stopnja=Decimal("22.00"),
        ddv_znesek=Decimal("-2.20"),
        skupaj=Decimal("-12.20"),
        ddv_nacin_obracuna="OBDAVCENO",
        ddv_eslog_koda=koda,
        ddv_rekap_skupina="OBDAVCENO",
    )

    with pytest.raises(ValueError, match="eSLOG kod"):
        potrdi_korekcijski_osnutek(dobropis)


@pytest.mark.parametrize(
    ("nacin", "koda"),
    [("OPROSCENO", "E"), ("OBRNJENA_DAVCNA", "AE")],
)
def test_dobropis_brez_ddv_klavzule_ne_porabi_stevilke(pogodba, nacin, koda):
    original = izdan_np(pogodba)
    dobropis = pripravi_korekcijski_osnutek(
        original, Invoice.DocumentType.DOBROPIS, "Popust", date(2026, 9, 2)
    )
    dodaj_vrstico(
        dobropis,
        opis="Dobropis brez klavzule",
        osnova=Decimal("-10.00"),
        ddv_stopnja=Decimal("0.00"),
        ddv_znesek=Decimal("0.00"),
        skupaj=Decimal("-10.00"),
        ddv_nacin_obracuna=nacin,
        ddv_eslog_koda=koda,
        ddv_klavzula="",
        ddv_rekap_skupina=nacin,
    )

    with pytest.raises(ValueError, match="DDV klavzule"):
        potrdi_korekcijski_osnutek(dobropis)

    assert dobropis.stevilka is None
    assert NumberSequence.objects.get(
        company=original.company, tip=NumberSequence.Tip.NP, leto=0
    ).zadnja_stevilka == 1


@pytest.mark.parametrize(
    "osnova,ddv,skupaj",
    [
        (float(-10), Decimal("-2.20"), Decimal("-12.20")),
        (Decimal("NaN"), Decimal("-2.20"), Decimal("-12.20")),
        (Decimal("-10.001"), Decimal("-2.20"), Decimal("-12.201")),
        (Decimal("-10.00"), Decimal("-2.20"), Decimal("-11.20")),
    ],
)
def test_dobropis_zavrne_neveljavne_rocne_zneske(pogodba, osnova, ddv, skupaj):
    original = izdan_np(pogodba)
    dobropis = pripravi_korekcijski_osnutek(
        original, Invoice.DocumentType.DOBROPIS, "Popust", date(2026, 9, 2)
    )
    with pytest.raises(ValidationError):
        dodaj_vrstico(
            dobropis,
            opis="Napaka",
            osnova=osnova,
            ddv_stopnja=Decimal("22.00"),
            ddv_znesek=ddv,
            skupaj=skupaj,
            ddv_nacin_obracuna="OBDAVCENO",
            ddv_eslog_koda="S",
            ddv_rekap_skupina="OBDAVCENO",
        )
    assert not InvoiceLine.objects.filter(invoice=dobropis).exists()


@pytest.mark.parametrize(
    "tip", [Invoice.DocumentType.STORNO, Invoice.DocumentType.DOBROPIS]
)
def test_korekcija_np_uporabi_np_zaporedje(pogodba, tip):
    original = izdan_np(pogodba)
    popravek = pripravi_korekcijski_osnutek(original, tip, "Napaka", date(2027, 1, 2))
    if tip == Invoice.DocumentType.DOBROPIS:
        dodaj_vrstico(
            popravek,
            opis="Dobropis",
            osnova=Decimal("-10.00"),
            ddv_stopnja=Decimal("0.00"),
            ddv_znesek=Decimal("0.00"),
            skupaj=Decimal("-10.00"),
            ddv_nacin_obracuna="NI_PREDMET",
            ddv_eslog_koda="O",
            ddv_rekap_skupina="NI_PREDMET",
        )
    assert popravek.stevilka is None
    assert potrdi_korekcijski_osnutek(popravek).stevilka == "NP-00002"
    assert list(
        NumberSequence.objects.filter(company=original.company, tip="NP").values_list("tip", "leto", "zadnja_stevilka")
    ) == [(NumberSequence.Tip.NP, 0, 2)]


@pytest.mark.parametrize(
    "tip", [Invoice.DocumentType.STORNO, Invoice.DocumentType.DOBROPIS]
)
def test_korekcija_st_uporabi_letno_st_zaporedje(pogodba, tip):
    np = izdan_np(pogodba)
    original = Invoice.objects.create(
        company=np.company,
        lease=np.lease,
        document_type=Invoice.DocumentType.ST,
        stevilka="ST-2026-0001",
        status=Invoice.Status.POSLAN,
        datum_izdaje=date(2026, 9, 1),
        obdobje_od=np.obdobje_od,
        obdobje_do=np.obdobje_do,
        izdajatelj_podpisnik_slika_pot=np.izdajatelj_podpisnik_slika_pot,
    )
    NumberSequence.objects.filter(company=np.company, tip="ST", leto=2026).update(zadnja_stevilka=1)
    InvoiceLine.objects.create(
        invoice=original,
        opis="Storitev",
        osnova=Decimal("20.00"),
        ddv_stopnja=Decimal("22.00"),
        ddv_znesek=Decimal("4.40"),
        skupaj=Decimal("24.40"),
        ddv_nacin_obracuna="OBDAVCENO",
        ddv_eslog_koda="S",
        ddv_rekap_skupina="OBDAVCENO",
    )
    popravek = pripravi_korekcijski_osnutek(original, tip, "Napaka", date(2027, 1, 2))
    if tip == Invoice.DocumentType.DOBROPIS:
        dodaj_vrstico(
            popravek,
            opis="Dobropis",
            osnova=Decimal("-5.00"),
            ddv_stopnja=Decimal("0.00"),
            ddv_znesek=Decimal("0.00"),
            skupaj=Decimal("-5.00"),
            ddv_nacin_obracuna="NI_PREDMET",
            ddv_eslog_koda="O",
            ddv_rekap_skupina="NI_PREDMET",
        )
    assert potrdi_korekcijski_osnutek(popravek).stevilka == "ST-2027-0001"
    assert list(
        NumberSequence.objects.filter(company=np.company, tip="ST")
        .order_by("leto")
        .values_list("leto", "zadnja_stevilka")
    ) == [(2026, 1), (2027, 1)]


def test_storno_vrstic_ni_mogoce_spremeniti_pred_potrditvijo(pogodba):
    original = izdan_np(pogodba)
    storno = pripravi_korekcijski_osnutek(
        original, Invoice.DocumentType.STORNO, "Napaka", date(2026, 9, 2)
    )
    vrstica = InvoiceLine.objects.filter(invoice=storno).first()
    with pytest.raises(ValueError):
        dodaj_vrstico(storno, opis="Nedovoljeno")
    with pytest.raises(ValueError):
        spremeni_vrstico(vrstica, opis="Nedovoljeno")
    with pytest.raises(ValueError):
        izbrisi_vrstico(vrstica)

    InvoiceLine.objects.filter(pk=vrstica.pk).update(opis="Nedovoljeno")
    with pytest.raises(ValueError, match="ne ujemajo"):
        potrdi_korekcijski_osnutek(storno)
    assert NumberSequence.objects.get(company=original.company, tip="NP").zadnja_stevilka == 1


def test_dobropis_nepotrjena_vrstica_mora_biti_konsistentna(pogodba):
    original = izdan_np(pogodba)
    dobropis = pripravi_korekcijski_osnutek(
        original, Invoice.DocumentType.DOBROPIS, "Popust", date(2026, 9, 2)
    )
    vrstica = dodaj_vrstico(
        dobropis,
        opis="Dobropis",
        osnova=Decimal("-10.00"),
        ddv_stopnja=Decimal("22.00"),
        ddv_znesek=Decimal("-2.20"),
        skupaj=Decimal("-12.20"),
        ddv_nacin_obracuna="OBDAVCENO",
        ddv_eslog_koda="S",
        ddv_rekap_skupina="OBDAVCENO",
    )
    with pytest.raises(ValidationError):
        spremeni_vrstico(vrstica, skupaj=Decimal("-11.20"))
    vrstica.refresh_from_db()
    assert vrstica.skupaj == Decimal("-12.20")

    InvoiceLine.objects.filter(pk=vrstica.pk).update(skupaj=Decimal("-11.20"))
    with pytest.raises(ValidationError):
        potrdi_korekcijski_osnutek(dobropis)
    assert NumberSequence.objects.get(company=original.company, tip="NP").zadnja_stevilka == 1


def test_napaka_po_stevilcenju_povrne_korekcijo_in_zaporedje(pogodba):
    original = izdan_np(pogodba)
    storno = pripravi_korekcijski_osnutek(
        original, Invoice.DocumentType.STORNO, "Napaka", date(2026, 9, 2)
    )
    with (
        patch.object(Invoice, "save", side_effect=RuntimeError("napaka")),
        pytest.raises(RuntimeError, match="napaka"),
    ):
        potrdi_korekcijski_osnutek(storno)
    storno.refresh_from_db()
    assert (storno.stevilka, storno.status, storno.skupaj_z_ddv) == (
        None,
        Invoice.Status.OSNUTEK,
        None,
    )
    assert storno.eslog_konfiguracija_snapshot is None
    assert NumberSequence.objects.get(company=original.company, tip="NP").zadnja_stevilka == 1


@pytest.mark.parametrize(
    "tip", [Invoice.DocumentType.STORNO, Invoice.DocumentType.DOBROPIS]
)
def test_np_osnutek_ne_postane_korekcija_s_splosno_storitvijo(pogodba, tip):
    original = izdan_np(pogodba)
    osnutek = pripravi_osnutek_np(
        pogodba, date(2026, 10, 1), date(2026, 10, 31), date(2026, 11, 1)
    ).invoice
    with pytest.raises(ValueError):
        spremeni_podatke_racuna(
            osnutek, document_type=tip, razlog="Popravek", povezan_racun=original
        )
    osnutek.refresh_from_db()
    assert (osnutek.document_type, osnutek.razlog, osnutek.povezan_racun_id) == (
        Invoice.DocumentType.NP,
        "",
        None,
    )


@pytest.mark.parametrize(
    "tip", [Invoice.DocumentType.STORNO, Invoice.DocumentType.DOBROPIS]
)
def test_korekcijskega_osnutka_ni_mogoce_pretipkati(pogodba, tip):
    original = izdan_np(pogodba)
    popravek = pripravi_korekcijski_osnutek(original, tip, "Napaka", date(2026, 9, 2))
    for spremembe in (
        {"document_type": Invoice.DocumentType.NP},
        {"povezan_racun": None},
        {"prejemnik_naziv": "Drug prejemnik"},
        {"osnova_skupaj": Decimal("1.00")},
    ):
        with pytest.raises(ValueError):
            spremeni_podatke_racuna(popravek, **spremembe)
    popravek.refresh_from_db()
    assert popravek.document_type == tip
    assert popravek.povezan_racun_id == original.pk
    assert popravek.prejemnik_naziv == original.prejemnik_naziv
    assert popravek.osnova_skupaj is None

    spremeni_podatke_racuna(
        popravek, datum_izdaje=date(2026, 9, 3), razlog="Popravljeno"
    )
    popravek.refresh_from_db()
    assert (popravek.datum_izdaje, popravek.razlog) == (date(2026, 9, 3), "Popravljeno")


@pytest.mark.parametrize(
    "tip", [Invoice.DocumentType.STORNO, Invoice.DocumentType.DOBROPIS]
)
def test_izdan_korekcijski_dokument_je_immutable(pogodba, tip):
    original = izdan_np(pogodba)
    popravek = pripravi_korekcijski_osnutek(original, tip, "Napaka", date(2026, 9, 2))
    if tip == Invoice.DocumentType.DOBROPIS:
        dodaj_vrstico(
            popravek,
            opis="Dobropis",
            osnova=Decimal("-5.00"),
            ddv_stopnja=Decimal("0.00"),
            ddv_znesek=Decimal("0.00"),
            skupaj=Decimal("-5.00"),
            ddv_nacin_obracuna="NI_PREDMET",
            ddv_eslog_koda="O",
            ddv_rekap_skupina="NI_PREDMET",
        )
    potrdi_korekcijski_osnutek(popravek)
    vrstica = InvoiceLine.objects.filter(invoice=popravek).first()
    with pytest.raises(ValueError):
        spremeni_podatke_racuna(popravek, razlog="Nov razlog")
    with pytest.raises(ValueError):
        spremeni_vrstico(vrstica, opis="Drug opis")
    with pytest.raises(ValueError):
        izbrisi_vrstico(vrstica)
    with pytest.raises(ValueError):
        izbrisi_racun(popravek)
    with pytest.raises(ValueError):
        potrdi_korekcijski_osnutek(popravek)
