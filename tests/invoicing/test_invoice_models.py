from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError

from catalogs.models import VatRegime
from companies.models import Company
from contracts.models import Lease, LeaseItem, Property, Tenant, Unit
from invoicing.models import Invoice, InvoiceLine
from tests.financial_fixtures import save_financial_fixture


@pytest.fixture
def company():
    return Company.objects.create(
        drzava_koda="SI",
        naziv="Alfa d.o.o.",
        davcna_stevilka="15012557",
        maticna_stevilka="1234567000",
        naslov="Glavna ulica 1",
        trr="SI56191000000123438",
        logotip="logo.png",
        email_racunovodja="racunovodja@example.com",
        email_posiljatelj="racuni@example.com",
        keychain_kljuc_posiljatelja="marjanapp-alfa",
    )


def podatki_racuna(company, **spremembe):
    podatki = {
        "company": company,
        "document_type": Invoice.DocumentType.NP,
        "datum_izdaje": date(2026, 9, 1),
        "obdobje_od": date(2026, 8, 1),
        "obdobje_do": date(2026, 8, 31),
        "prejemnik_naziv": "Najemnik d.o.o.",
        "prejemnik_naslov": "Trg 1",
        "izdajatelj_naziv": company.naziv,
        "izdajatelj_naslov": company.naslov,
        "izdajatelj_davcna_stevilka": company.davcna_stevilka,
    }
    return podatki | spremembe


def podatki_vrstice(invoice, **spremembe):
    podatki = {
        "invoice": invoice,
        "opis": "Najemnina za avgust 2026",
        "osnova": Decimal("100.00"),
        "ddv_stopnja": Decimal("22.00"),
        "ddv_znesek": Decimal("22.00"),
        "skupaj": Decimal("122.00"),
        "ddv_nacin_obracuna": "OBDAVCENO",
        "ddv_klavzula": "",
        "ddv_rekap_skupina": "STANDARD",
    }
    return podatki | spremembe


def ustvari_pogodbo(company):
    property = Property.objects.create(
        company=company, naziv="Poslovna stavba", naslov="Glavna ulica 1"
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
    return Lease.objects.create(
        unit=unit,
        tenant=tenant,
        oznaka_pogodbe="P-1",
        tip_trajanja=Lease.TipTrajanja.NEDOLOCEN_CAS,
        datum_zacetka=date(2026, 1, 1),
        rok_placila_dni=15,
    )


@pytest.mark.django_db
def test_invoice_polja_izbire_in_privzeti_status(company):
    assert set(Invoice.DocumentType.values) == {"NP", "ST", "STORNO", "DOBROPIS"}
    assert set(Invoice.Status.values) == {
        "OSNUTEK",
        "ZA_PONOVNI_PREGLED",
        "IZBRAN_ZA_POSILJANJE",
        "POSLAN",
        "NAPAKA",
        "REZULTAT_NI_ZNAN",
        "ARHIVIRAN_NI_IZDAN",
        "STORNIRAN",
        "POPRAVLJEN",
    }
    assert {field.name for field in Invoice._meta.concrete_fields} == {
        "id",
        "company",
        "document_type",
        "lease",
        "stevilka",
        "datum_izdaje",
        "obdobje_od",
        "obdobje_do",
        "rok_placila",
        "prejemnik_naziv",
        "prejemnik_naslov",
        "prejemnik_drzava_koda",
        "prejemnik_identificiran_za_ddv",
        "prejemnik_polni_odbitek",
        "prejemnik_davcna_stevilka",
        "prejemnik_ddv_identifikacijska_stevilka",
        "prejemnik_maticna_stevilka",
        "izdajatelj_naziv",
        "izdajatelj_naslov",
        "izdajatelj_drzava_koda",
        "izdajatelj_identificiran_za_ddv",
        "izdajatelj_davcna_stevilka",
        "izdajatelj_maticna_stevilka",
        "izdajatelj_trr",
        "izdajatelj_logotip_slika_pot",
        "pdf_template_version",
        "izdajatelj_podpisnik_ime",
        "izdajatelj_podpisnik_funkcija",
        "izdajatelj_podpisnik_slika_pot",
        "oznaka_pogodbe",
        "objekt_naziv",
        "status",
        "povezan_racun",
        "razlog",
        "razlog_arhiviranja",
        "osnova_skupaj",
        "ddv_skupaj",
        "skupaj_z_ddv",
        "rendered_pdf_path",
        "rendered_eslog_path",
        "checksum",
        "prejemnik_tip",
        "rendered_eslog_checksum",
        "eslog_konfiguracija_snapshot",
        "dostavni_kontakti_snapshot",
    }
    invoice = Invoice(**podatki_racuna(company))
    invoice.full_clean()
    invoice.save()
    assert invoice.status == Invoice.Status.OSNUTEK
    assert invoice.stevilka is None
    assert invoice.rok_placila is None
    assert invoice.osnova_skupaj is None


@pytest.mark.django_db
@pytest.mark.parametrize("document_type", Invoice.DocumentType.values)
def test_invoice_document_type_je_veljaven(company, document_type):
    invoice = Invoice(
        **podatki_racuna(company, document_type=document_type, razlog="Popravek")
    )
    invoice.full_clean()


@pytest.mark.django_db
def test_invoice_neveljavne_izbire(company):
    invoice = Invoice(**podatki_racuna(company, document_type="DRUGO"))
    with pytest.raises(ValidationError) as error:
        invoice.full_clean()
    assert "document_type" in error.value.message_dict

    invoice.document_type = Invoice.DocumentType.NP
    invoice.status = "DRUGO"
    with pytest.raises(ValidationError) as error:
        invoice.full_clean()
    assert "status" in error.value.message_dict


@pytest.mark.django_db
@pytest.mark.parametrize(
    "document_type", [Invoice.DocumentType.STORNO, Invoice.DocumentType.DOBROPIS]
)
def test_storno_in_dobropis_zahtevata_razlog(company, document_type):
    invoice = Invoice(
        **podatki_racuna(company, document_type=document_type, razlog="  ")
    )
    with pytest.raises(ValidationError) as error:
        invoice.full_clean()
    assert "razlog" in error.value.message_dict

    invoice.razlog = "Napačna osnova"
    invoice.full_clean()


@pytest.mark.django_db
def test_st_brez_pogodbe_je_dovoljen(company):
    invoice = Invoice(**podatki_racuna(company, document_type=Invoice.DocumentType.ST))
    invoice.full_clean()
    invoice.save()
    assert invoice.lease is None


@pytest.mark.django_db
def test_ista_stevilka_pri_istem_podjetju_ni_dovoljena(company):
    Invoice.objects.create(**podatki_racuna(company, stevilka="NP-2026-1"))
    with pytest.raises(IntegrityError), transaction.atomic():
        Invoice.objects.create(**podatki_racuna(company, stevilka="NP-2026-1"))


@pytest.mark.django_db
def test_vec_osnutkov_brez_stevilke_je_dovoljenih(company):
    for _ in range(2):
        Invoice.objects.create(**podatki_racuna(company, stevilka=None))
    assert Invoice.objects.filter(company=company, stevilka__isnull=True).count() == 2


@pytest.mark.django_db
def test_ista_stevilka_pri_dveh_podjetjih_je_dovoljena(company):
    drugo = Company.objects.create(
        drzava_koda="SI",
        naziv="Beta d.o.o.",
        davcna_stevilka=company.davcna_stevilka,
        maticna_stevilka=company.maticna_stevilka,
        naslov="Druga ulica 2",
        trr=company.trr,
        logotip="logo.png",
        email_racunovodja="beta@example.com",
        email_posiljatelj="beta@example.com",
        keychain_kljuc_posiljatelja="marjanapp-beta",
        aktivno=False,
    )
    Invoice.objects.create(**podatki_racuna(company, stevilka="NP-2026-1"))
    Invoice.objects.create(**podatki_racuna(drugo, stevilka="NP-2026-1"))
    assert Invoice.objects.filter(stevilka="NP-2026-1").count() == 2


@pytest.mark.django_db
def test_invoice_company_fk_je_protected(company):
    Invoice.objects.create(**podatki_racuna(company))
    with pytest.raises(ProtectedError):
        company.delete()


@pytest.mark.django_db
def test_invoice_lease_in_povezan_racun_fk_sta_protected(company):
    lease = ustvari_pogodbo(company)
    original = Invoice.objects.create(**podatki_racuna(company, lease=lease))
    Invoice.objects.create(
        **podatki_racuna(
            company,
            document_type=Invoice.DocumentType.STORNO,
            povezan_racun=original,
            razlog="Razveljavitev",
        )
    )
    with pytest.raises(ProtectedError):
        lease.delete()
    with pytest.raises(ProtectedError):
        original.delete()


@pytest.mark.django_db
def test_invoice_line_polja_in_nullable_lease_item(company):
    invoice = Invoice.objects.create(
        **podatki_racuna(company, document_type=Invoice.DocumentType.ST)
    )
    assert {field.name for field in InvoiceLine._meta.concrete_fields} == {
        "id",
        "invoice",
        "lease",
        "lease_item",
        "adjustment",
        "adjustment_snapshot",
        "rent_indexation_event",
        "opis",
        "osnova",
        "ddv_stopnja",
        "ddv_znesek",
        "skupaj",
        "ddv_nacin_obracuna",
        "ddv_rezim_sifra",
        "ddv_zahteva_polni_odbitek",
        "ddv_klavzula",
        "ddv_rekap_skupina",
        "ddv_eslog_koda",
        "je_sorazmerno",
        "razlog_rocnega_popravka",
        "izvirno_izracunana_vrednost",
    }
    line = InvoiceLine(**podatki_vrstice(invoice))
    line.full_clean()
    line.save()
    line.refresh_from_db()
    assert line.lease_item is None
    assert line.adjustment_id is None
    assert line.adjustment_snapshot == {}
    assert line.rent_indexation_event_id is None
    assert line.lease is None
    assert line.je_sorazmerno is False
    line.je_sorazmerno = True
    line.save(update_fields=["je_sorazmerno"])
    line.refresh_from_db()
    assert line.je_sorazmerno is True
    assert line.osnova == Decimal("100.00")


@pytest.mark.django_db
def test_invoice_line_povezave_so_protected(company):
    lease = ustvari_pogodbo(company)
    vat_regime = VatRegime.objects.create(
        zahteva_polni_odbitek=False,
        sifra="DDV22",
        naziv="Splošna stopnja",
        nacin_obracuna=VatRegime.NacinObracuna.OBDAVCENO,
        stopnja=Decimal("22.00"),
        rekapitulacijska_skupina="STANDARD",
        velja_od=date(2026, 1, 1),
    )
    lease_item = LeaseItem.objects.create(
        lease=lease,
        naziv_postavke="Najemnina",
        znesek_neto_mesecno=Decimal("100.00"),
        vat_regime=vat_regime,
    )
    invoice = Invoice.objects.create(**podatki_racuna(company))
    InvoiceLine.objects.create(
        **podatki_vrstice(invoice, lease=lease, lease_item=lease_item)
    )
    with pytest.raises(ProtectedError):
        invoice.delete()
    with pytest.raises(ProtectedError):
        lease.delete()
    with pytest.raises(ProtectedError):
        lease_item.delete()


@pytest.mark.django_db
def test_snapshot_vrstice_ostane_po_spremembi_lease_item_in_ddv(company):
    lease = ustvari_pogodbo(company)
    vat_regime = VatRegime.objects.create(
        zahteva_polni_odbitek=False,
        sifra="DDV22",
        naziv="Splošna stopnja",
        nacin_obracuna=VatRegime.NacinObracuna.OBDAVCENO,
        stopnja=Decimal("22.00"),
        klavzula="Prvotna klavzula",
        rekapitulacijska_skupina="STANDARD",
        velja_od=date(2026, 1, 1),
    )
    lease_item = LeaseItem.objects.create(
        lease=lease,
        naziv_postavke="Najemnina",
        znesek_neto_mesecno=Decimal("100.00"),
        vat_regime=vat_regime,
    )
    invoice = Invoice.objects.create(**podatki_racuna(company))
    line = InvoiceLine.objects.create(
        **podatki_vrstice(
            invoice, lease=lease, lease_item=lease_item, ddv_klavzula="Prvotna klavzula"
        )
    )
    lease_item.znesek_neto_mesecno = Decimal("200.00")
    save_financial_fixture(lease_item)
    vat_regime.stopnja = Decimal("9.50")
    vat_regime.klavzula = "Nova klavzula"
    save_financial_fixture(vat_regime)

    line.refresh_from_db()
    assert line.opis == "Najemnina za avgust 2026"
    assert line.osnova == Decimal("100.00")
    assert line.ddv_stopnja == Decimal("22.00")
    assert line.ddv_znesek == Decimal("22.00")
    assert line.skupaj == Decimal("122.00")
    assert line.ddv_klavzula == "Prvotna klavzula"


@pytest.mark.django_db
def test_razlicna_izvirna_osnova_zahteva_razlog_popravka(company):
    invoice = Invoice.objects.create(**podatki_racuna(company))
    line = InvoiceLine(
        **podatki_vrstice(
            invoice,
            izvirno_izracunana_vrednost=Decimal("90.00"),
            razlog_rocnega_popravka="  ",
        )
    )
    with pytest.raises(ValidationError) as error:
        line.full_clean()
    assert "razlog_rocnega_popravka" in error.value.message_dict

    line.razlog_rocnega_popravka = "Dogovorjen popravek"
    line.full_clean()


@pytest.mark.django_db
def test_enaka_izvirna_osnova_ne_pomeni_nujno_popravka(company):
    invoice = Invoice.objects.create(**podatki_racuna(company))
    line = InvoiceLine(
        **podatki_vrstice(invoice, izvirno_izracunana_vrednost=Decimal("100.00"))
    )
    line.full_clean()


@pytest.mark.django_db
def test_invoice_line_lease_fk_je_protected(company):
    lease = ustvari_pogodbo(company)
    invoice = Invoice.objects.create(**podatki_racuna(company))
    InvoiceLine.objects.create(**podatki_vrstice(invoice, lease=lease))
    with pytest.raises(ProtectedError):
        lease.delete()


@pytest.mark.django_db
def test_invoice_line_save_ne_preracuna_zneskov(company):
    invoice = Invoice.objects.create(**podatki_racuna(company))
    line = InvoiceLine.objects.create(**podatki_vrstice(invoice))
    line.osnova = Decimal("110.00")
    line.save()
    line.refresh_from_db()
    assert line.osnova == Decimal("110.00")
    assert line.ddv_znesek == Decimal("22.00")
    assert line.skupaj == Decimal("122.00")


@pytest.mark.django_db
def test_manjkajoca_osnova_ne_dokazuje_rocnega_popravka(company):
    invoice = Invoice.objects.create(**podatki_racuna(company))
    line = InvoiceLine(
        **podatki_vrstice(
            invoice,
            osnova=None,
            izvirno_izracunana_vrednost=Decimal("100.00"),
        )
    )
    line.clean()
