from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db.models.deletion import ProtectedError

from companies.models import Company
from contracts.models import Lease, Property, Tenant, Unit


def ustvari_podjetje():
    return Company.objects.create(
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


def podatki_najemnika(**spremembe):
    podatki = {
        "tip": Tenant.Tip.PRAVNA_OSEBA,
        "naziv": "Najemnik d.o.o.",
        "davcna_stevilka": "15012557",
        "maticna_stevilka": "1234567000",
        "naslov": "Trg 1",
        "email": "najemnik@example.com",
    }
    return podatki | spremembe


@pytest.mark.django_db
def test_property_in_unit_polja_privzete_vrednosti_in_natancnost():
    company = ustvari_podjetje()
    nepremicnina = Property.objects.create(
        company=company,
        naziv="Poslovna stavba",
        naslov="Glavna ulica 1",
    )
    enota = Unit.objects.create(
        property=nepremicnina,
        oznaka="Pisarna 1",
        tip_enote=Unit.TipEnote.PISARNA,
        povrsina_m2=Decimal("42.50"),
    )

    assert nepremicnina.aktivna is True
    assert enota.aktivna is True
    povrsina = Unit._meta.get_field("povrsina_m2")
    assert (povrsina.max_digits, povrsina.decimal_places) == (8, 2)


@pytest.mark.django_db
def test_tip_enote_je_zaprt_textchoices_nabor():
    assert set(Unit.TipEnote.values) == {
        "PISARNA",
        "POSLOVNI_PROSTOR",
        "SKLADISCE",
        "PARKIRNO_MESTO",
        "GARAZA",
        "OPREMA",
        "DRUGO",
    }
    nepremicnina = Property.objects.create(
        company=ustvari_podjetje(),
        naziv="Poslovna stavba",
        naslov="Glavna ulica 1",
    )
    enota = Unit(property=nepremicnina, oznaka="X", tip_enote="NEVELJAVNO")

    with pytest.raises(ValidationError):
        enota.full_clean()


@pytest.mark.django_db
def test_poslovni_fk_preprecijo_kaskadni_izbris():
    company = ustvari_podjetje()
    nepremicnina = Property.objects.create(
        company=company,
        naziv="Poslovna stavba",
        naslov="Glavna ulica 1",
    )
    Unit.objects.create(
        property=nepremicnina,
        oznaka="Pisarna 1",
        tip_enote=Unit.TipEnote.PISARNA,
    )

    with pytest.raises(ProtectedError):
        company.delete()
    with pytest.raises(ProtectedError):
        nepremicnina.delete()


@pytest.mark.django_db
def test_pravna_oseba_zahteva_davcno_in_maticno_stevilko():
    najemnik = Tenant(
        **podatki_najemnika(davcna_stevilka="", maticna_stevilka="")
    )

    with pytest.raises(ValidationError) as napaka:
        najemnik.full_clean()

    assert set(napaka.value.message_dict) == {
        "davcna_stevilka",
        "maticna_stevilka",
    }


@pytest.mark.django_db
def test_fizicna_oseba_ne_zahteva_davcne_in_maticne_stevilke():
    najemnik = Tenant(
        **podatki_najemnika(
            tip=Tenant.Tip.FIZICNA_OSEBA,
            naziv="Ana Novak",
            davcna_stevilka="",
            maticna_stevilka="",
        )
    )

    najemnik.full_clean()


@pytest.mark.django_db
def test_maticna_stevilka_mora_imeti_natanko_deset_mest():
    najemnik = Tenant(**podatki_najemnika(maticna_stevilka="123456789"))

    with pytest.raises(ValidationError, match="natanko 10 mest"):
        najemnik.full_clean()


@pytest.mark.django_db
def test_tenant_tip_je_zaprt_textchoices_nabor_in_email_je_veljaven():
    assert set(Tenant.Tip.values) == {"PRAVNA_OSEBA", "FIZICNA_OSEBA"}
    najemnik = Tenant(**podatki_najemnika(tip="NEVELJAVNO", email="ni-email"))

    with pytest.raises(ValidationError) as napaka:
        najemnik.full_clean()

    assert {"tip", "email"} <= set(napaka.value.message_dict)



def ustvari_pogodbeni_stranki(rok_placila_dni=15):
    company = ustvari_podjetje()
    company.rok_placila_dni_privzeto = rok_placila_dni
    company.save(update_fields=["rok_placila_dni_privzeto"])
    nepremicnina = Property.objects.create(
        company=company,
        naziv="Poslovna stavba",
        naslov="Glavna ulica 1",
    )
    enota = Unit.objects.create(
        property=nepremicnina,
        oznaka="Pisarna 1",
        tip_enote=Unit.TipEnote.PISARNA,
    )
    najemnik = Tenant.objects.create(**podatki_najemnika())
    return enota, najemnik


def nova_pogodba(**spremembe):
    enota, najemnik = ustvari_pogodbeni_stranki()
    podatki = {
        "unit": enota,
        "tenant": najemnik,
        "oznaka_pogodbe": "P-2026-001",
        "tip_trajanja": Lease.TipTrajanja.NEDOLOCEN_CAS,
        "datum_zacetka": date(2026, 1, 1),
    }
    return Lease(**(podatki | spremembe))


@pytest.mark.django_db
def test_lease_textchoices_in_privzete_vrednosti():
    assert set(Lease.TipTrajanja.values) == {"DOLOCEN_CAS", "NEDOLOCEN_CAS"}
    assert set(Lease.Status.values) == {"AKTIVNA", "ODPOVEDANA", "PRENEHALA"}
    assert set(Lease.TipPogojaIndeksacije.values) == {
        "VEDNO",
        "PRAG_SPREMEMBE",
        "SAMO_NAVZGOR",
    }

    pogodba = nova_pogodba()
    pogodba.full_clean()

    assert pogodba.samodejno_podaljsanje is False
    assert pogodba.status == Lease.Status.AKTIVNA
    assert pogodba.arhivirana is False
    assert pogodba.ima_klavzulo_indeksacije is False


@pytest.mark.django_db
def test_rok_placila_ob_ustvarjanju_privzame_podjetje_in_ostane_nastavljiv():
    enota, najemnik = ustvari_pogodbeni_stranki(rok_placila_dni=22)
    skupni = {
        "unit": enota,
        "tenant": najemnik,
        "tip_trajanja": Lease.TipTrajanja.NEDOLOCEN_CAS,
        "datum_zacetka": date(2026, 1, 1),
    }
    pogodba = Lease.objects.create(oznaka_pogodbe="P-1", **skupni)
    rocna = Lease.objects.create(
        oznaka_pogodbe="P-2", rok_placila_dni=30, **skupni
    )

    assert pogodba.rok_placila_dni == 22
    assert rocna.rok_placila_dni == 30


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("tip_trajanja", "datum_konca"),
    [
        (Lease.TipTrajanja.DOLOCEN_CAS, None),
        (Lease.TipTrajanja.NEDOLOCEN_CAS, date(2026, 12, 31)),
    ],
)
def test_tip_trajanja_doloca_datum_konca(tip_trajanja, datum_konca):
    pogodba = nova_pogodba(
        tip_trajanja=tip_trajanja,
        datum_konca=datum_konca,
    )

    with pytest.raises(ValidationError) as napaka:
        pogodba.full_clean()

    assert "datum_konca" in napaka.value.message_dict


@pytest.mark.django_db
def test_samodejno_podaljsanje_zahteva_obdobje():
    pogodba = nova_pogodba(samodejno_podaljsanje=True)

    with pytest.raises(ValidationError) as napaka:
        pogodba.full_clean()

    assert "obdobje_podaljsanja_mesecev" in napaka.value.message_dict


@pytest.mark.django_db
def test_indeksacijska_klavzula_zahteva_tip_in_prag_kadar_ustreza():
    brez_tipa = nova_pogodba(ima_klavzulo_indeksacije=True)
    with pytest.raises(ValidationError) as napaka:
        brez_tipa.full_clean()
    assert "tip_pogoja_indeksacije" in napaka.value.message_dict

    brez_tipa.tip_pogoja_indeksacije = (
        Lease.TipPogojaIndeksacije.PRAG_SPREMEMBE
    )
    with pytest.raises(ValidationError) as napaka:
        brez_tipa.full_clean()
    assert "prag_spremembe_odstotek" in napaka.value.message_dict

    brez_tipa.prag_spremembe_odstotek = Decimal("2.00")
    brez_tipa.full_clean()


@pytest.mark.django_db
def test_datum_prenehanja_status_arhiviranje_in_poslovna_fk():
    pogodba = nova_pogodba(
        datum_prenehanja=date(2026, 6, 30),
        status=Lease.Status.AKTIVNA,
        arhivirana=True,
    )
    pogodba.full_clean()
    pogodba.save()

    assert pogodba.status == Lease.Status.AKTIVNA
    assert pogodba.arhivirana is True
    with pytest.raises(ProtectedError):
        pogodba.unit.delete()
    with pytest.raises(ProtectedError):
        pogodba.tenant.delete()

