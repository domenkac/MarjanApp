from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest

from catalogs.models import VatRegime
from companies.models import Company, NumberSequence, Signatory
from contracts.models import Lease, LeaseItem, Property, Tenant, Unit
from invoicing.models import Invoice, InvoiceLine
from invoicing.services import potrdi_osnutek_np, pripravi_osnutek_np

OBDOBJE_OD = date(2026, 8, 1)
OBDOBJE_DO = date(2026, 8, 31)
DATUM_IZDAJE = date(2026, 9, 1)


@pytest.fixture
def pogodba(db):
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
    rezim = VatRegime.objects.create(
        sifra=f"R{VatRegime.objects.count() + 1}",
        naziv=nacin,
        nacin_obracuna=nacin,
        stopnja=Decimal("22.00"),
        klavzula="Davčna klavzula" if nacin != "OBDAVCENO" else "",
        rekapitulacijska_skupina=nacin,
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
