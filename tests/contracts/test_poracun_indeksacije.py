from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db.models.deletion import ProtectedError

from catalogs.models import VatRegime
from companies.models import Company
from contracts.models import (
    IndexationEvent,
    Lease,
    LeaseItem,
    PoracunIndeksacije,
    Property,
    Tenant,
    Unit,
)
from invoicing.models import Invoice


@pytest.fixture
def poracun_podatki():
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
    )
    property = Property.objects.create(
        company=company, naziv="Poslovna stavba", naslov="Glavna ulica 1"
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
    lease = Lease.objects.create(
        unit=unit,
        tenant=tenant,
        oznaka_pogodbe="P-1",
        tip_trajanja=Lease.TipTrajanja.NEDOLOCEN_CAS,
        datum_zacetka=date(2026, 1, 1),
        rok_placila_dni=15,
    )
    vat_regime = VatRegime.objects.create(
        sifra="DDV22",
        naziv="Splošna stopnja",
        nacin_obracuna=VatRegime.NacinObracuna.OBDAVCENO,
        stopnja=Decimal("22.00"),
        rekapitulacijska_skupina="STANDARD",
        velja_od=date(2026, 1, 1),
    )
    lease_item = LeaseItem.objects.create(
        lease=lease,
        znesek_neto_mesecno=Decimal("100.00"),
        vat_regime=vat_regime,
    )
    event = IndexationEvent.objects.create(
        lease_item=lease_item,
        datum_uskladitve=date(2026, 2, 1),
        indeks_vrednost=Decimal("110.000"),
        stara_vrednost=Decimal("100.00"),
        nova_vrednost=Decimal("110.00"),
    )
    original = Invoice.objects.create(
        company=company,
        document_type=Invoice.DocumentType.NP,
        stevilka="NP-2026-1",
        datum_izdaje=date(2026, 2, 1),
        obdobje_od=date(2026, 2, 1),
        obdobje_do=date(2026, 2, 28),
        status=Invoice.Status.POSLAN,
    )
    return {
        "indexation_event": event,
        "original_invoice": original,
        "prizadeto_obdobje_od": date(2026, 2, 1),
        "prizadeto_obdobje_do": date(2026, 2, 28),
        "znesek_poracuna": Decimal("10.00"),
    }


@pytest.mark.django_db
def test_polja_privzeti_status_in_nullable_racun(poracun_podatki):
    assert {field.name for field in PoracunIndeksacije._meta.concrete_fields} == {
        "id",
        "indexation_event",
        "original_invoice",
        "prizadeto_obdobje_od",
        "prizadeto_obdobje_do",
        "znesek_poracuna",
        "status",
        "razlog_zavrnitve",
        "vkljucen_v_racun",
    }
    assert set(PoracunIndeksacije.Status.values) == {
        "PREDLAGAN", "POTRJEN", "ZAVRNJEN"
    }
    poracun = PoracunIndeksacije(**poracun_podatki)
    poracun.full_clean()
    poracun.save()
    poracun.refresh_from_db()
    assert poracun.status == PoracunIndeksacije.Status.PREDLAGAN
    assert poracun.vkljucen_v_racun is None
    assert poracun.znesek_poracuna == Decimal("10.00")


@pytest.mark.django_db
@pytest.mark.parametrize(
    "status", [PoracunIndeksacije.Status.PREDLAGAN, PoracunIndeksacije.Status.POTRJEN]
)
def test_razlog_zavrnitve_ni_obvezen_pri_drugih_statusih(poracun_podatki, status):
    PoracunIndeksacije(**poracun_podatki, status=status).full_clean()


@pytest.mark.django_db
def test_zavrnjen_zahteva_razlog(poracun_podatki):
    poracun = PoracunIndeksacije(
        **poracun_podatki,
        status=PoracunIndeksacije.Status.ZAVRNJEN,
        razlog_zavrnitve="  ",
    )
    with pytest.raises(ValidationError) as error:
        poracun.full_clean()
    assert "razlog_zavrnitve" in error.value.message_dict

    poracun.razlog_zavrnitve = "Poračun ni upravičen"
    poracun.full_clean()


@pytest.mark.django_db
def test_neveljaven_status_je_zavrnjen(poracun_podatki):
    poracun = PoracunIndeksacije(**poracun_podatki, status="DRUGO")
    with pytest.raises(ValidationError) as error:
        poracun.full_clean()
    assert "status" in error.value.message_dict


@pytest.mark.django_db
def test_vsi_poslovni_fk_so_protected(poracun_podatki):
    original = poracun_podatki["original_invoice"]
    included = Invoice.objects.create(
        company=original.company,
        document_type=Invoice.DocumentType.NP,
        datum_izdaje=date(2026, 3, 1),
        obdobje_od=date(2026, 3, 1),
        obdobje_do=date(2026, 3, 31),
    )
    poracun = PoracunIndeksacije.objects.create(
        **poracun_podatki, vkljucen_v_racun=included
    )
    assert poracun.vkljucen_v_racun == included
    for referenced in (poracun.indexation_event, original, included):
        with pytest.raises(ProtectedError):
            referenced.delete()


@pytest.mark.django_db
def test_napacen_znesek_poracuna_je_zavrnjen(poracun_podatki):
    podatki = poracun_podatki | {"znesek_poracuna": Decimal("9.99")}
    poracun = PoracunIndeksacije(**podatki)
    with pytest.raises(ValidationError) as error:
        poracun.full_clean()
    assert "znesek_poracuna" in error.value.message_dict


@pytest.mark.django_db
def test_osnutek_brez_stevilke_ni_originalni_racun(poracun_podatki):
    original = poracun_podatki["original_invoice"]
    original.stevilka = None
    original.status = Invoice.Status.OSNUTEK
    original.save(update_fields=["stevilka", "status"])
    poracun = PoracunIndeksacije(**poracun_podatki)
    with pytest.raises(ValidationError) as error:
        poracun.full_clean()
    assert "original_invoice" in error.value.message_dict
