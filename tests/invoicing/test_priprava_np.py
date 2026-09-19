from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError

from catalogs.models import VatRegime
from companies.models import Company, NumberSequence, Signatory
from contracts.models import Lease, LeaseItem, Property, Tenant, Unit
from invoicing.models import Invoice, InvoiceLine
from invoicing.rendering import nastavi_eslog_artifact
from invoicing.services import (
    dodaj_vrstico,
    izbrisi_racun,
    izbrisi_vrstico,
    potrdi_osnutek_np,
    preveri_uredljiv_osnutek,
    pripravi_osnutek_np,
    spremeni_podatke_racuna,
    spremeni_vrstico,
)

OBDOBJE_OD = date(2026, 8, 1)
OBDOBJE_DO = date(2026, 8, 31)
DATUM_IZDAJE = date(2026, 9, 1)


@pytest.fixture
def pogodba(db, tmp_path, settings):
    settings.MEDIA_ROOT = tmp_path
    (tmp_path / "logo.png").write_bytes(b"logo")
    (tmp_path / "podpis.png").write_bytes(b"podpis")
    company = Company.objects.create(
        naziv="Alfa d.o.o.",
        davcna_stevilka="15012557",
        maticna_stevilka="1234567000",
        naslov="Glavna ulica 1",
        trr="SI56191000000123438",
        logotip="logo.png",
        email_racunovodja="racunovodja@example.com",
        email_posiljatelj="racuni@example.com",
        keychain_kljuc_posiljatelja="marjanapp-alfa",
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
        company=company, naziv="Poslovna stavba A", naslov="Glavna ulica 1"
    )
    unit = Unit.objects.create(
        property=property, oznaka="Pisarna 1", tip_enote=Unit.TipEnote.PISARNA
    )
    tenant = Tenant.objects.create(
        tip=Tenant.Tip.PRAVNA_OSEBA,
        naziv="Najemnik d.o.o.",
        davcna_stevilka="15012557",
        maticna_stevilka="1234567000",
        naslov="Trg 1",
        email="najemnik@example.com",
    )
    return Lease.objects.create(
        unit=unit,
        tenant=tenant,
        oznaka_pogodbe="12/2024",
        tip_trajanja=Lease.TipTrajanja.NEDOLOCEN_CAS,
        datum_zacetka=date(2026, 1, 1),
        rok_placila_dni=15,
    )


def dodaj_postavko(pogodba, *, naziv="Najemnina", znesek="100.00", nacin="OBDAVCENO", **spremembe):
    eslog_kode = {
        "OBDAVCENO": "S",
        "OPROSCENO": "E",
        "OBRNJENA_DAVCNA": "AE",
        "NI_PREDMET": "O",
    }
    rezim = VatRegime.objects.create(
        sifra=f"R{VatRegime.objects.count() + 1}",
        naziv=nacin,
        nacin_obracuna=nacin,
        stopnja=Decimal("22.00"),
        klavzula="Davčna klavzula" if nacin != "OBDAVCENO" else "",
        rekapitulacijska_skupina=nacin,
        eslog_koda=eslog_kode[nacin],
        velja_od=date(2026, 1, 1),
    )
    return LeaseItem.objects.create(
        lease=pogodba,
        naziv_postavke=naziv,
        znesek_neto_mesecno=Decimal(znesek),
        vat_regime=rezim,
        **spremembe,
    )


def pripravi(pogodba):
    return pripravi_osnutek_np(pogodba, OBDOBJE_OD, OBDOBJE_DO, DATUM_IZDAJE)


def test_ena_postavka_in_snapshote_ohrani_po_spremembah(pogodba):
    item = dodaj_postavko(pogodba)
    rezultat = pripravi(pogodba)
    invoice = rezultat.invoice
    assert rezultat.razlog_neizdaje is None
    assert invoice.company == pogodba.unit.property.company
    assert invoice.lease == pogodba
    assert invoice.document_type == Invoice.DocumentType.NP
    assert (invoice.datum_izdaje, invoice.obdobje_od, invoice.obdobje_do) == (
        DATUM_IZDAJE, OBDOBJE_OD, OBDOBJE_DO
    )
    assert invoice.status == Invoice.Status.OSNUTEK
    assert (invoice.stevilka, invoice.rok_placila) == (None, None)
    assert (invoice.osnova_skupaj, invoice.ddv_skupaj, invoice.skupaj_z_ddv) == (
        None, None, None
    )
    assert (invoice.prejemnik_naziv, invoice.prejemnik_naslov) == (
        "Najemnik d.o.o.", "Trg 1"
    )
    assert invoice.prejemnik_davcna_stevilka == "15012557"
    assert invoice.prejemnik_maticna_stevilka == "1234567000"
    assert (invoice.izdajatelj_naziv, invoice.izdajatelj_naslov) == (
        "Alfa d.o.o.", "Glavna ulica 1"
    )
    assert invoice.izdajatelj_davcna_stevilka == "15012557"
    assert invoice.izdajatelj_maticna_stevilka == "1234567000"
    assert invoice.izdajatelj_trr == "SI56191000000123438"
    assert (
        invoice.izdajatelj_podpisnik_ime,
        invoice.izdajatelj_podpisnik_funkcija,
        invoice.izdajatelj_podpisnik_slika_pot,
    ) == ("Ana Podpisnica", "Direktorica", "podpis.png")
    assert (invoice.oznaka_pogodbe, invoice.objekt_naziv) == (
        "12/2024", "Poslovna stavba A"
    )

    line = InvoiceLine.objects.get(invoice=invoice)
    assert (line.lease, line.lease_item) == (pogodba, item)
    assert line.opis == (
        "Najemnina za obdobje 01.08.2026 – 31.08.2026\n"
        "Pisarna 1, Poslovna stavba A · Pogodba št. 12/2024"
    )
    assert (line.osnova, line.ddv_stopnja, line.ddv_znesek, line.skupaj) == (
        Decimal("100.00"), Decimal("22.00"), Decimal("22.00"), Decimal("122.00")
    )
    assert line.ddv_nacin_obracuna == "OBDAVCENO"
    assert line.ddv_rekap_skupina == "OBDAVCENO"
    assert line.je_sorazmerno is False

    pogodba.tenant.naziv = "Spremenjen najemnik"
    pogodba.tenant.save()
    pogodba.unit.property.company.naziv = "Spremenjen izdajatelj"
    pogodba.unit.property.company.save()
    podpisnik = Signatory.objects.get(company=pogodba.unit.property.company)
    podpisnik.ime_priimek = "Spremenjena podpisnica"
    podpisnik.save()
    item.vat_regime.stopnja = Decimal("9.50")
    item.vat_regime.klavzula = "Nova klavzula"
    item.vat_regime.save()
    invoice.refresh_from_db()
    line.refresh_from_db()
    assert invoice.prejemnik_naziv == "Najemnik d.o.o."
    assert invoice.izdajatelj_naziv == "Alfa d.o.o."
    assert invoice.izdajatelj_podpisnik_ime == "Ana Podpisnica"
    assert line.ddv_stopnja == Decimal("22.00")
    assert line.ddv_znesek == Decimal("22.00")
    assert line.ddv_klavzula == ""


def test_vec_postavk_z_mesanim_ddv_in_sledljivostjo(pogodba):
    najemnina = dodaj_postavko(pogodba)
    parkirisce = dodaj_postavko(
        pogodba, naziv="Parkirišče", znesek="30.00", nacin="OPROSCENO"
    )
    invoice = pripravi(pogodba).invoice
    lines = {line.lease_item_id: line for line in InvoiceLine.objects.filter(invoice=invoice)}
    assert set(lines) == {najemnina.pk, parkirisce.pk}
    assert all(line.lease_id == pogodba.pk for line in lines.values())
    assert lines[najemnina.pk].ddv_znesek == Decimal("22.00")
    assert lines[parkirisce.pk].ddv_znesek == Decimal("0.00")
    assert lines[parkirisce.pk].ddv_nacin_obracuna == "OPROSCENO"
    assert lines[parkirisce.pk].ddv_klavzula == "Davčna klavzula"
    assert lines[parkirisce.pk].skupaj == Decimal("30.00")


def test_sorazmerna_in_neveljavni_postavki(pogodba):
    aktivna = dodaj_postavko(
        pogodba, znesek="310.00", datum_zacetka_postavke=date(2026, 8, 16)
    )
    dodaj_postavko(pogodba, datum_zacetka_postavke=date(2026, 9, 1))
    dodaj_postavko(pogodba, arhivirana=True)
    invoice = pripravi(pogodba).invoice
    line = InvoiceLine.objects.get(invoice=invoice)
    assert line.lease_item == aktivna
    assert line.osnova == Decimal("160.00")
    assert line.ddv_znesek == Decimal("35.20")
    assert line.je_sorazmerno is True


def test_brez_obracunljivih_postavk_ne_ustvari_racuna(pogodba):
    dodaj_postavko(pogodba, arhivirana=True)
    rezultat = pripravi(pogodba)
    assert rezultat.invoice is None
    assert rezultat.razlog_neizdaje
    assert not Invoice.objects.exists()
    assert not InvoiceLine.objects.exists()


def test_zunaj_obdobja_pogodbe_ne_ustvari_racuna(pogodba):
    dodaj_postavko(pogodba)
    pogodba.datum_zacetka = date(2026, 9, 1)
    pogodba.save()
    rezultat = pripravi(pogodba)
    assert rezultat.invoice is None
    assert rezultat.razlog_neizdaje == "Pogodba ne velja v tem obdobju"
    assert not Invoice.objects.exists()


def test_napaka_pri_vrsticah_povrne_cel_osnutek(pogodba):
    dodaj_postavko(pogodba)
    with (
        patch.object(InvoiceLine.objects, "bulk_create", side_effect=RuntimeError("napaka")),
        pytest.raises(RuntimeError, match="napaka"),
    ):
        pripravi(pogodba)
    assert not Invoice.objects.exists()



@pytest.mark.parametrize("razlog", ["brez_podpisnika", "prekrivanje", "ddv_ni_potrjen"])
def test_predpogoji_blokirajo_pripravo(pogodba, razlog):
    dodaj_postavko(pogodba)
    company = pogodba.unit.property.company
    if razlog == "brez_podpisnika":
        Signatory.objects.filter(company=company).delete()
    elif razlog == "prekrivanje":
        Signatory.objects.create(
            company=company,
            ime_priimek="Drugi podpisnik",
            podpis_slika="drugi.png",
            velja_od=date(2026, 8, 1),
            velja_do=date(2026, 9, 30),
        )
    else:
        company.ddv_razporeditev_potrjena = False
        company.save()
    with pytest.raises(ValueError):
        pripravi(pogodba)
    assert not Invoice.objects.exists()


def test_potrditev_ene_vrstice_ohrani_snapshote_in_nastavi_status(pogodba):
    postavka = dodaj_postavko(pogodba)
    invoice = pripravi(pogodba).invoice
    prvotni_naziv = invoice.prejemnik_naziv
    prvotna_vrstica = InvoiceLine.objects.get(invoice=invoice)
    pogodba.tenant.naziv = "Spremenjen najemnik"
    pogodba.tenant.save(update_fields=["naziv"])
    postavka.naziv_postavke = "Spremenjena postavka"
    postavka.save(update_fields=["naziv_postavke"])

    potrjen = potrdi_osnutek_np(invoice)

    assert potrjen.stevilka == "NP-00001"
    assert potrjen.status == Invoice.Status.IZBRAN_ZA_POSILJANJE
    assert (potrjen.osnova_skupaj, potrjen.ddv_skupaj, potrjen.skupaj_z_ddv) == (
        Decimal("100.00"), Decimal("22.00"), Decimal("122.00")
    )
    assert potrjen.rok_placila == date(2026, 9, 16)
    assert potrjen.prejemnik_naziv == prvotni_naziv
    assert InvoiceLine.objects.get(pk=prvotna_vrstica.pk).opis == prvotna_vrstica.opis


@pytest.mark.parametrize(
    ("nacin", "koda"),
    [("OBDAVCENO", "S"), ("OPROSCENO", "E"), ("OBRNJENA_DAVCNA", "AE"), ("NI_PREDMET", "O")],
)
def test_potrditev_shrani_eslog_snapshote(pogodba, nacin, koda):
    postavka = dodaj_postavko(pogodba, nacin=nacin)
    invoice = pripravi(pogodba).invoice
    assert invoice.prejemnik_tip == ""
    assert InvoiceLine.objects.get(invoice=invoice).ddv_eslog_koda == ""

    potrjen = potrdi_osnutek_np(invoice)

    assert potrjen.prejemnik_tip == Tenant.Tip.PRAVNA_OSEBA
    assert InvoiceLine.objects.get(invoice=invoice).ddv_eslog_koda == koda

    pogodba.tenant.tip = Tenant.Tip.FIZICNA_OSEBA
    pogodba.tenant.save(update_fields=["tip"])
    postavka.vat_regime.eslog_koda = "SPREMENJENA"
    postavka.vat_regime.save(update_fields=["eslog_koda"])
    potrjen.refresh_from_db()
    assert potrjen.prejemnik_tip == Tenant.Tip.PRAVNA_OSEBA
    assert InvoiceLine.objects.get(invoice=invoice).ddv_eslog_koda == koda


@pytest.mark.parametrize("koda", ["", "E"])
def test_potrditev_zavrne_manjkajoco_ali_napacno_eslog_kodo(pogodba, koda):
    postavka = dodaj_postavko(pogodba)
    postavka.vat_regime.eslog_koda = koda
    postavka.vat_regime.save(update_fields=["eslog_koda"])
    invoice = pripravi(pogodba).invoice

    with pytest.raises(ValueError, match="eSLOG kod"):
        potrdi_osnutek_np(invoice)

    invoice.refresh_from_db()
    assert invoice.stevilka is None
    assert InvoiceLine.objects.get(invoice=invoice).ddv_eslog_koda == ""

def test_fizicna_oseba_brez_davcne_stevilke_se_lahko_izda(pogodba):
    pogodba.tenant.tip = Tenant.Tip.FIZICNA_OSEBA
    pogodba.tenant.davcna_stevilka = ""
    pogodba.tenant.maticna_stevilka = ""
    pogodba.tenant.save(update_fields=["tip", "davcna_stevilka", "maticna_stevilka"])
    dodaj_postavko(pogodba)

    potrjen = potrdi_osnutek_np(pripravi(pogodba).invoice)

    assert potrjen.prejemnik_tip == Tenant.Tip.FIZICNA_OSEBA
    assert potrjen.prejemnik_davcna_stevilka == ""


def test_eslog_artifact_se_nastavi_samo_z_namensko_storitvijo(pogodba):
    dodaj_postavko(pogodba)
    invoice = potrdi_osnutek_np(pripravi(pogodba).invoice)

    shranjen = nastavi_eslog_artifact(
        invoice, rendered_eslog_path="racun.xml", checksum="abc123"
    )
    assert (shranjen.rendered_eslog_path, shranjen.rendered_eslog_checksum) == (
        "racun.xml", "abc123"
    )
    assert nastavi_eslog_artifact(
        invoice, rendered_eslog_path="racun.xml", checksum="abc123"
    ).pk == invoice.pk
    with pytest.raises(ValueError, match="že vsebuje eSLOG"):
        nastavi_eslog_artifact(
            invoice, rendered_eslog_path="drug.xml", checksum="drug"
        )


def test_potrditev_vec_vrstic_sesteje_shranjene_zneske_in_rok_pogodbe(pogodba):
    pogodba.rok_placila_dni = 23
    pogodba.save(update_fields=["rok_placila_dni"])
    dodaj_postavko(pogodba)
    dodaj_postavko(pogodba, naziv="Parkirišče", znesek="30.00", nacin="OPROSCENO")
    invoice = pripravi(pogodba).invoice

    potrjen = potrdi_osnutek_np(invoice)

    assert (potrjen.osnova_skupaj, potrjen.ddv_skupaj, potrjen.skupaj_z_ddv) == (
        Decimal("130.00"), Decimal("22.00"), Decimal("152.00")
    )
    assert potrjen.rok_placila == date(2026, 9, 24)


def test_potrditvi_porabita_zaporedni_np_stevilki_brez_letnega_reseta(pogodba):
    dodaj_postavko(pogodba)
    prvi = pripravi(pogodba).invoice
    drugi = pripravi(pogodba).invoice
    drugi.datum_izdaje = date(2027, 1, 1)
    drugi.save(update_fields=["datum_izdaje"])

    assert potrdi_osnutek_np(prvi).stevilka == "NP-00001"
    assert potrdi_osnutek_np(drugi).stevilka == "NP-00002"
    zaporedje = NumberSequence.objects.get(company=prvi.company)
    assert (zaporedje.tip, zaporedje.leto, zaporedje.zadnja_stevilka) == (
        NumberSequence.Tip.NP, 0, 2
    )


def test_ponovna_potrditev_ne_porabi_druge_stevilke(pogodba):
    dodaj_postavko(pogodba)
    invoice = pripravi(pogodba).invoice
    potrdi_osnutek_np(invoice)

    with pytest.raises(ValueError):
        potrdi_osnutek_np(invoice)

    invoice.refresh_from_db()
    assert invoice.stevilka == "NP-00001"
    assert NumberSequence.objects.get().zadnja_stevilka == 1


def test_neostevilcen_racun_z_drugim_statusom_je_zavrnjen(pogodba):
    dodaj_postavko(pogodba)
    invoice = pripravi(pogodba).invoice
    invoice.status = Invoice.Status.ZA_PONOVNI_PREGLED
    invoice.save(update_fields=["status"])

    with pytest.raises(ValueError):
        potrdi_osnutek_np(invoice)

    assert not NumberSequence.objects.exists()

def test_ostevilcen_osnutek_se_ne_popravlja_tiho(pogodba):
    dodaj_postavko(pogodba)
    invoice = pripravi(pogodba).invoice
    invoice.stevilka = "NP-00042"
    invoice.save(update_fields=["stevilka"])

    with pytest.raises(ValueError):
        potrdi_osnutek_np(invoice)

    assert not NumberSequence.objects.exists()


def test_osnutek_brez_vrstic_je_zavrnjen(pogodba):
    dodaj_postavko(pogodba)
    invoice = pripravi(pogodba).invoice
    InvoiceLine.objects.filter(invoice=invoice).delete()

    with pytest.raises(ValueError):
        potrdi_osnutek_np(invoice)

    assert not NumberSequence.objects.exists()


def test_drug_tip_dokumenta_je_zavrnjen(pogodba):
    dodaj_postavko(pogodba)
    invoice = pripravi(pogodba).invoice
    invoice.document_type = Invoice.DocumentType.ST
    invoice.save(update_fields=["document_type"])

    with pytest.raises(ValueError):
        potrdi_osnutek_np(invoice)

    assert not NumberSequence.objects.exists()


def test_napaka_po_dodelitvi_stevilke_povrne_racun_in_zaporedje(pogodba):
    dodaj_postavko(pogodba)
    invoice = pripravi(pogodba).invoice
    NumberSequence.objects.create(
        company=invoice.company, tip=NumberSequence.Tip.NP, leto=0,
        zadnja_stevilka=41,
    )

    with (
        patch.object(Invoice, "save", side_effect=RuntimeError("napaka po dodelitvi")),
        pytest.raises(RuntimeError, match="napaka po dodelitvi"),
    ):
        potrdi_osnutek_np(invoice)

    invoice.refresh_from_db()
    assert (invoice.stevilka, invoice.status, invoice.rok_placila) == (
        None, Invoice.Status.OSNUTEK, None
    )
    assert (invoice.osnova_skupaj, invoice.ddv_skupaj, invoice.skupaj_z_ddv) == (
        None, None, None
    )
    assert NumberSequence.objects.get().zadnja_stevilka == 41


def test_np_potrditev_zavrne_neskladno_vsoto_vrstice(pogodba):
    dodaj_postavko(pogodba)
    invoice = pripravi(pogodba).invoice
    vrstica = InvoiceLine.objects.get(invoice=invoice)
    spremeni_vrstico(vrstica, skupaj=Decimal("999.00"))

    with pytest.raises(ValueError, match="vsota"):
        potrdi_osnutek_np(invoice)

    invoice.refresh_from_db()
    assert (invoice.stevilka, invoice.status) == (None, Invoice.Status.OSNUTEK)
    assert not NumberSequence.objects.exists()


def test_np_potrditev_zavrne_vrstico_brez_izvorne_postavke(pogodba):
    dodaj_postavko(pogodba)
    invoice = pripravi(pogodba).invoice
    InvoiceLine.objects.filter(invoice=invoice).update(lease_item=None)

    with pytest.raises(ValueError, match="postavko"):
        potrdi_osnutek_np(invoice)

    assert not NumberSequence.objects.exists()


def test_np_potrditev_zavrne_podjetje_drugo_od_pogodbe(pogodba):
    dodaj_postavko(pogodba)
    invoice = pripravi(pogodba).invoice
    izvirno = invoice.company
    drugo = Company.objects.create(
        naziv="Druga d.o.o.",
        davcna_stevilka=izvirno.davcna_stevilka,
        maticna_stevilka=izvirno.maticna_stevilka,
        naslov=izvirno.naslov,
        trr=izvirno.trr,
        logotip=izvirno.logotip.name,
        email_racunovodja=izvirno.email_racunovodja,
        email_posiljatelj=izvirno.email_posiljatelj,
        keychain_kljuc_posiljatelja=izvirno.keychain_kljuc_posiljatelja,
        aktivno=False,
    )
    spremeni_podatke_racuna(invoice, company=drugo)

    with pytest.raises(ValueError, match="podjetje"):
        potrdi_osnutek_np(invoice)

    assert not NumberSequence.objects.exists()


def test_neostevilcen_osnutek_se_lahko_ureja(pogodba):
    dodaj_postavko(pogodba)
    invoice = pripravi(pogodba).invoice
    assert preveri_uredljiv_osnutek(invoice).pk == invoice.pk

    spremeni_podatke_racuna(invoice, prejemnik_naziv="Popravljen najemnik")
    nova = dodaj_vrstico(
        invoice,
        opis="Ročna vrstica",
        osnova=Decimal("10.00"),
        ddv_stopnja=Decimal("0.00"),
        ddv_znesek=Decimal("0.00"),
        skupaj=Decimal("10.00"),
        ddv_nacin_obracuna="OPROSCENO",
        ddv_rekap_skupina="OPROSCENO",
    )
    spremeni_vrstico(nova, opis="Popravljena vrstica")
    nova.refresh_from_db()
    assert nova.opis == "Popravljena vrstica"
    izbrisi_vrstico(nova)

    invoice.refresh_from_db()
    assert invoice.prejemnik_naziv == "Popravljen najemnik"
    assert not InvoiceLine.objects.filter(pk=nova.pk).exists()
    with pytest.raises(ValueError, match="poslovna in posnetna polja"):
        spremeni_podatke_racuna(invoice, status=Invoice.Status.POSLAN)
    with pytest.raises(ValueError, match="poslovna in posnetna polja"):
        spremeni_podatke_racuna(invoice, rendered_pdf_path="racun.pdf")


def test_ostevilcen_racun_in_vrstice_se_ne_spremenijo(pogodba):
    dodaj_postavko(pogodba)
    invoice = pripravi(pogodba).invoice
    potrdi_osnutek_np(invoice)
    assert invoice.stevilka is None  # preveri tudi zastarel objekt v pomnilniku
    vrstica = InvoiceLine.objects.get(invoice=invoice)
    prvotni_naziv = invoice.prejemnik_naziv
    prvotni_opis = vrstica.opis
    stevilo_vrstic = InvoiceLine.objects.count()

    with pytest.raises(ValueError, match="neoštevilčen osnutek"):
        preveri_uredljiv_osnutek(invoice)
    with pytest.raises(ValueError, match="neoštevilčen osnutek"):
        spremeni_podatke_racuna(invoice, prejemnik_naziv="Nedovoljena sprememba")
    with pytest.raises(ValueError, match="neoštevilčen osnutek"):
        dodaj_vrstico(invoice, opis="Nedovoljena vrstica")
    with pytest.raises(ValueError, match="neoštevilčen osnutek"):
        spremeni_vrstico(vrstica, opis="Nedovoljena sprememba")
    with pytest.raises(ValueError, match="neoštevilčen osnutek"):
        izbrisi_vrstico(vrstica)
    with pytest.raises(ValueError, match="neoštevilčen osnutek"):
        izbrisi_racun(invoice)

    invoice.refresh_from_db()
    vrstica.refresh_from_db()
    assert invoice.prejemnik_naziv == prvotni_naziv
    assert vrstica.opis == prvotni_opis
    assert InvoiceLine.objects.count() == stevilo_vrstic
    assert Invoice.objects.filter(pk=invoice.pk).exists()


def test_neostevilcen_racun_z_drugim_statusom_ni_uredljiv(pogodba):
    dodaj_postavko(pogodba)
    invoice = pripravi(pogodba).invoice
    invoice.status = Invoice.Status.ZA_PONOVNI_PREGLED
    invoice.save(update_fields=["status"])

    with pytest.raises(ValueError, match="neoštevilčen osnutek"):
        spremeni_podatke_racuna(invoice, prejemnik_naziv="Nedovoljena sprememba")
    invoice.refresh_from_db()
    assert invoice.prejemnik_naziv == "Najemnik d.o.o."


@pytest.mark.parametrize(
    "document_type", [Invoice.DocumentType.STORNO, Invoice.DocumentType.DOBROPIS]
)
def test_sprememba_racuna_brez_razloga_ne_obide_validacije(pogodba, document_type):
    dodaj_postavko(pogodba)
    invoice = pripravi(pogodba).invoice

    with pytest.raises(ValidationError) as error:
        spremeni_podatke_racuna(invoice, document_type=document_type)

    assert "razlog" in error.value.message_dict
    invoice.refresh_from_db()
    assert invoice.document_type == Invoice.DocumentType.NP
    assert invoice.razlog == ""


def test_sprememba_in_dodajanje_vrstice_brez_razloga_ne_obideta_validacije(pogodba):
    dodaj_postavko(pogodba)
    invoice = pripravi(pogodba).invoice
    vrstica = InvoiceLine.objects.get(invoice=invoice)
    stevilo_vrstic = InvoiceLine.objects.count()

    with pytest.raises(ValidationError) as error:
        spremeni_vrstico(vrstica, izvirno_izracunana_vrednost=Decimal("90.00"))
    assert "razlog_rocnega_popravka" in error.value.message_dict

    with pytest.raises(ValidationError) as error:
        dodaj_vrstico(
            invoice,
            opis="Ročno popravljena vrstica",
            osnova=Decimal("100.00"),
            ddv_stopnja=Decimal("22.00"),
            ddv_znesek=Decimal("22.00"),
            skupaj=Decimal("122.00"),
            ddv_nacin_obracuna="OBDAVCENO",
            ddv_rekap_skupina="OBDAVCENO",
            izvirno_izracunana_vrednost=Decimal("90.00"),
        )
    assert "razlog_rocnega_popravka" in error.value.message_dict

    vrstica.refresh_from_db()
    assert vrstica.izvirno_izracunana_vrednost is None
    assert vrstica.razlog_rocnega_popravka is None
    assert InvoiceLine.objects.count() == stevilo_vrstic


def test_priprava_zavrne_postavko_brez_obvezne_reference(pogodba):
    postavka = dodaj_postavko(pogodba)
    postavka.vat_regime.zahteva_referenco = True
    postavka.vat_regime.save(update_fields=["zahteva_referenco"])

    with pytest.raises(ValidationError) as error:
        pripravi(pogodba)

    assert {"referenca_listine", "datum_reference"} <= set(error.value.message_dict)
    assert not Invoice.objects.exists()
