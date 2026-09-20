import base64
from datetime import date
from decimal import Decimal

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from catalogs.models import VatRegime
from companies.models import Company, NumberSequence, Signatory
from contracts.models import Lease, LeaseItem, Property, Tenant, Unit
from invoicing.models import Invoice, InvoiceLine
from invoicing.services import (
    dodaj_vrstico,
    potrdi_korekcijski_osnutek,
    potrdi_osnutek_np,
    pripravi_korekcijski_osnutek,
    pripravi_osnutek_np,
)
from rendering.contexts import (
    PdfRenderNapaka,
    pripravi_pdf_context,
    pripravi_vzorcni_pdf_context,
)

VALID_PNG_LOGO = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg=="
)
VALID_PNG_PODPIS = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYPj/HwADAgH/5ncLrgAAAABJRU5ErkJggg=="
)
VALID_PNG_NOV_ASSET = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNg+M/wHwAEAQH/cetH5QAAAABJRU5ErkJggg=="
)


@pytest.fixture
def izdani_racun(db, tmp_path, settings):
    settings.MEDIA_ROOT = tmp_path
    (tmp_path / "logo.png").write_bytes(VALID_PNG_LOGO)
    (tmp_path / "podpis.png").write_bytes(VALID_PNG_PODPIS)
    company = Company.objects.create(
        drzava_koda="SI",
        naziv="Alfa d.o.o.",
        davcna_stevilka="15012557",
        maticna_stevilka="1234567000",
        naslov="Glavna 1",
        trr="SI56191000000123438",
        logotip="logo.png",
        email_racunovodja="racunovodja@example.com",
        email_posiljatelj="racuni@example.com",
        keychain_kljuc_posiljatelja="alfa",
        ddv_razporeditev_potrjena=True,
    )
    Signatory.objects.create(
        company=company,
        ime_priimek="Ana Podpisnica",
        funkcija="Direktorica",
        podpis_slika="podpis.png",
        velja_od=date(2026, 1, 1),
    )
    property = Property.objects.create(
        company=company, naziv="Stavba A", naslov="Glavna 1"
    )
    unit = Unit.objects.create(
        property=property, oznaka="Pisarna 1", tip_enote=Unit.TipEnote.PISARNA
    )
    tenant = Tenant.objects.create(
        drzava_koda="SI",
        identificiran_za_ddv=True,
        tip=Tenant.Tip.PRAVNA_OSEBA,
        naziv="Najemnik d.o.o.",
        davcna_stevilka="15012557",
        maticna_stevilka="1234567000",
        naslov="Trg 1",
        email="najemnik@example.com",
    )
    lease = Lease.objects.create(
        unit=unit,
        tenant=tenant,
        oznaka_pogodbe="12/2024",
        tip_trajanja=Lease.TipTrajanja.NEDOLOCEN_CAS,
        datum_zacetka=date(2026, 1, 1),
        rok_placila_dni=15,
    )
    regime = VatRegime.objects.create(
        sifra="R1",
        naziv="DDV 22",
        nacin_obracuna=VatRegime.NacinObracuna.OBDAVCENO,
        stopnja=Decimal("22.00"),
        rekapitulacijska_skupina="DDV 22 %",
        eslog_koda="S",
        velja_od=date(2026, 1, 1),
    )
    LeaseItem.objects.create(
        lease=lease,
        naziv_postavke="Najemnina",
        znesek_neto_mesecno=Decimal("100.00"),
        vat_regime=regime,
    )
    invoice = pripravi_osnutek_np(
        lease, date(2026, 8, 1), date(2026, 8, 31), date(2026, 9, 1)
    ).invoice
    return potrdi_osnutek_np(invoice)


def test_pdf_context_uporablja_shranjene_snapshote(izdani_racun):
    context = pripravi_pdf_context(izdani_racun)
    assert context.stevilka == "NP-00001"
    assert context.prejemnik_naziv == "Najemnik d.o.o."
    assert context.izdajatelj_naziv == "Alfa d.o.o."
    assert context.izdajatelj_logotip_slika_pot.startswith("nespremenljivi-asseti/")
    assert context.izdajatelj_podpisnik_ime == "Ana Podpisnica"
    assert context.izdajatelj_podpisnik_slika_pot.startswith("nespremenljivi-asseti/")
    assert context.pdf_template_version == "1"
    assert context.vrstice[0].osnova == Decimal("100.00")
    assert context.vrstice[0].ddv_znesek == Decimal("22.00")
    assert context.skupaj_z_ddv == Decimal("122.00")


@pytest.mark.parametrize("vir", ["company", "tenant", "vat_regime", "signatory"])
def test_sprememba_zivega_vira_ne_spremeni_pdf_contexta(izdani_racun, vir):
    pred = pripravi_pdf_context(izdani_racun)
    lease = izdani_racun.lease
    if vir == "company":
        company = lease.unit.property.company
        company.naziv = "Novo podjetje"
        company.logotip = "nov-logo.png"
        company.save(update_fields=["naziv", "logotip"])
    elif vir == "tenant":
        tenant = lease.tenant
        tenant.naziv = "Nov najemnik"
        tenant.save(update_fields=["naziv"])
    elif vir == "vat_regime":
        regime = LeaseItem.objects.get(lease=lease).vat_regime
        regime.stopnja = Decimal("9.50")
        regime.save(update_fields=["stopnja"])
    else:
        signatory = Signatory.objects.get(company=izdani_racun.company)
        signatory.ime_priimek = "Nova podpisnica"
        signatory.podpis_slika = "nov-podpis.png"
        signatory.save(update_fields=["ime_priimek", "podpis_slika"])
    assert pripravi_pdf_context(izdani_racun) == pred


def test_neostevilcen_racun_je_zavrnjen(izdani_racun):
    Invoice.objects.filter(pk=izdani_racun.pk).update(stevilka=None)
    with pytest.raises(PdfRenderNapaka) as error:
        pripravi_pdf_context(izdani_racun)
    assert error.value.koda == "STEVILKA_MANJKA"


def test_racun_brez_vrstic_je_zavrnjen(izdani_racun):
    InvoiceLine.objects.filter(invoice=izdani_racun).delete()
    with pytest.raises(PdfRenderNapaka) as error:
        pripravi_pdf_context(izdani_racun)
    assert error.value.koda == "VRSTICE_MANJKAJO"


@pytest.mark.parametrize(
    "polje", ["prejemnik_naziv", "izdajatelj_logotip_slika_pot", "pdf_template_version"]
)
def test_manjkajoci_snapshot_je_zavrnjen(izdani_racun, polje):
    Invoice.objects.filter(pk=izdani_racun.pk).update(**{polje: ""})
    with pytest.raises(PdfRenderNapaka) as error:
        pripravi_pdf_context(izdani_racun)
    assert error.value.koda == "SNAPSHOT_MANJKA"


def test_neskladni_totali_so_zavrnjeni(izdani_racun):
    Invoice.objects.filter(pk=izdani_racun.pk).update(skupaj_z_ddv=Decimal("123.00"))
    with pytest.raises(PdfRenderNapaka) as error:
        pripravi_pdf_context(izdani_racun)
    assert error.value.koda == "VSOTE_NESKLADNE"


def test_vzorcni_context_ne_ustvari_racuna_in_ne_porabi_stevilke(izdani_racun):
    racunov = Invoice.objects.count()
    zaporedje = NumberSequence.objects.get(company=izdani_racun.company)
    stevilka = zaporedje.zadnja_stevilka

    context = pripravi_vzorcni_pdf_context(
        izdani_racun.lease,
        datum_izdaje=date(2026, 10, 1),
        obdobje_od=date(2026, 9, 1),
        obdobje_do=date(2026, 9, 30),
    )

    assert context.oznaka_vzorca == "VZOREC – NI RAČUN"
    assert context.stevilka is None
    assert context.vrstice[0].osnova == Decimal("100.00")
    assert Invoice.objects.count() == racunov
    assert InvoiceLine.objects.count() == 1
    zaporedje.refresh_from_db()
    assert zaporedje.zadnja_stevilka == stevilka


def test_neobstojec_racun_je_zavrnjen(db):
    with pytest.raises(PdfRenderNapaka) as error:
        pripravi_pdf_context(None)
    assert error.value.koda == "RACUN_NE_OBSTAJA"


def test_manjkajoci_snapshot_asset_je_zavrnjen(izdani_racun, tmp_path):
    pot = Invoice.objects.get(pk=izdani_racun.pk).izdajatelj_logotip_slika_pot
    (tmp_path / pot).unlink()
    with pytest.raises(PdfRenderNapaka) as error:
        pripravi_pdf_context(izdani_racun)
    assert error.value.koda == "ASSET_MANJKA"


def test_korekcija_ob_izdaji_shrani_render_snapshota(izdani_racun):
    osnutek = pripravi_korekcijski_osnutek(
        izdani_racun, Invoice.DocumentType.STORNO, "Popravek", date(2026, 9, 2)
    )
    popravek = potrdi_korekcijski_osnutek(osnutek)
    popravek.refresh_from_db()
    assert (
        popravek.izdajatelj_logotip_slika_pot
        == izdani_racun.izdajatelj_logotip_slika_pot
    )
    assert popravek.pdf_template_version == "1"


@pytest.mark.parametrize("tip", [Invoice.DocumentType.NP, Invoice.DocumentType.ST])
def test_np_in_st_brez_roka_placila_sta_zavrnjena(izdani_racun, tip):
    Invoice.objects.filter(pk=izdani_racun.pk).update(
        document_type=tip,
        stevilka="ST-2026-0001"
        if tip == Invoice.DocumentType.ST
        else izdani_racun.stevilka,
        rok_placila=None,
    )

    with pytest.raises(PdfRenderNapaka) as error:
        pripravi_pdf_context(izdani_racun)

    assert error.value.koda == "SNAPSHOT_MANJKA"


@pytest.mark.parametrize(
    "tip", [Invoice.DocumentType.STORNO, Invoice.DocumentType.DOBROPIS]
)
def test_korekcija_brez_roka_placila_ohrani_referenco_in_razlog(izdani_racun, tip):
    osnutek = pripravi_korekcijski_osnutek(
        izdani_racun, tip, "Popravek zneska", date(2026, 9, 2)
    )
    if tip == Invoice.DocumentType.DOBROPIS:
        dodaj_vrstico(
            osnutek,
            opis="Dobropis",
            osnova=Decimal("-10.00"),
            ddv_stopnja=Decimal("0.00"),
            ddv_znesek=Decimal("0.00"),
            skupaj=Decimal("-10.00"),
            ddv_nacin_obracuna="NI_PREDMET",
            ddv_eslog_koda="O",
            ddv_rekap_skupina="NI_PREDMET",
        )
    popravek = potrdi_korekcijski_osnutek(osnutek)
    assert popravek.rok_placila is None

    context = pripravi_pdf_context(popravek)

    assert context.document_type == tip
    assert context.rok_placila is None
    assert context.povezan_racun_stevilka == izdani_racun.stevilka
    assert context.razlog == "Popravek zneska"


def _asset_bytes(pot):
    with default_storage.open(pot, "rb") as datoteka:
        return datoteka.read()


@pytest.mark.parametrize(
    ("polje", "izvorna_pot", "pricakovano"),
    [
        ("izdajatelj_logotip_slika_pot", "logo.png", VALID_PNG_LOGO),
        ("izdajatelj_podpisnik_slika_pot", "podpis.png", VALID_PNG_PODPIS),
    ],
)
def test_fizicni_prepis_trenutnega_asseta_ne_spremeni_snapshot_bytes(
    izdani_racun, tmp_path, polje, izvorna_pot, pricakovano
):
    snapshot_pot = getattr(Invoice.objects.get(pk=izdani_racun.pk), polje)
    (tmp_path / izvorna_pot).write_bytes(VALID_PNG_NOV_ASSET)

    assert _asset_bytes(snapshot_pot) == pricakovano


def test_nova_logo_in_podpis_datoteka_ne_spremenita_snapshot_bytes(izdani_racun):
    invoice = Invoice.objects.get(pk=izdani_racun.pk)
    logo_pot = invoice.izdajatelj_logotip_slika_pot
    podpis_pot = invoice.izdajatelj_podpisnik_slika_pot
    company = invoice.company
    podpisnik = Signatory.objects.get(company=company)

    company.logotip.save("nov-logo.png", ContentFile(VALID_PNG_NOV_ASSET), save=True)
    podpisnik.podpis_slika.save(
        "nov-podpis.png", ContentFile(VALID_PNG_NOV_ASSET), save=True
    )

    assert _asset_bytes(logo_pot) == VALID_PNG_LOGO
    assert _asset_bytes(podpis_pot) == VALID_PNG_PODPIS
    invoice.refresh_from_db()
    assert invoice.izdajatelj_logotip_slika_pot == logo_pot
    assert invoice.izdajatelj_podpisnik_slika_pot == podpis_pot


@pytest.mark.parametrize("dvoumno", [False, True])
def test_vzorec_vrne_pdf_napako_za_neveljavnega_podpisnika(izdani_racun, dvoumno):
    Signatory.objects.filter(company=izdani_racun.company).delete()
    if dvoumno:
        Signatory.objects.create(
            company=izdani_racun.company,
            ime_priimek="Prvi",
            podpis_slika="podpis.png",
            velja_od=date(2026, 1, 1),
            velja_do=date(2026, 12, 31),
        )
        Signatory.objects.create(
            company=izdani_racun.company,
            ime_priimek="Drugi",
            podpis_slika="podpis.png",
            velja_od=date(2026, 1, 1),
        )

    with pytest.raises(PdfRenderNapaka) as error:
        pripravi_vzorcni_pdf_context(
            izdani_racun.lease,
            datum_izdaje=date(2026, 10, 1),
            obdobje_od=date(2026, 9, 1),
            obdobje_do=date(2026, 9, 30),
        )

    assert error.value.koda in {"VZOREC_NEVELJAVEN", "PODPISNIK_NEVELJAVEN"}
