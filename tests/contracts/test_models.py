from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db.models.deletion import ProtectedError

from companies.models import Company
from contracts.models import Property, Tenant, Unit


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
