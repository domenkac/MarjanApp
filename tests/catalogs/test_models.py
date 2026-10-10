from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError

from catalogs.models import ServiceCatalogItem, VatRegime


def podatki_rezima(**spremembe):
    podatki = {
        "sifra": "DDV22",
        "zahteva_polni_odbitek": False,
        "naziv": "DDV 22 %",
        "nacin_obracuna": VatRegime.NacinObracuna.OBDAVCENO,
        "stopnja": Decimal("22.00"),
        "rekapitulacijska_skupina": "DDV 22 %",
        "velja_od": date(2026, 1, 1),
    }
    return podatki | spremembe


@pytest.mark.django_db
@pytest.mark.parametrize(
    "nacin_obracuna",
    [
        VatRegime.NacinObracuna.OPROSCENO,
        VatRegime.NacinObracuna.OBRNJENA_DAVCNA,
    ],
)
def test_klavzula_je_obvezna_za_predpisana_rezima(nacin_obracuna):
    rezim = VatRegime(**podatki_rezima(nacin_obracuna=nacin_obracuna))

    with pytest.raises(ValidationError) as napaka:
        rezim.full_clean()

    assert napaka.value.message_dict["klavzula"] == [
        "Klavzula je obvezna za izbrani način obračuna."
    ]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "nacin_obracuna",
    [VatRegime.NacinObracuna.OBDAVCENO, VatRegime.NacinObracuna.NI_PREDMET],
)
def test_klavzula_ni_obvezna_za_druga_rezima(nacin_obracuna):
    rezim = VatRegime(**podatki_rezima(nacin_obracuna=nacin_obracuna))

    rezim.full_clean()


@pytest.mark.django_db
def test_nacin_obracuna_je_zaprt_textchoices_nabor():
    assert set(VatRegime.NacinObracuna.values) == {
        "OBDAVCENO",
        "OPROSCENO",
        "NI_PREDMET",
        "OBRNJENA_DAVCNA",
    }
    rezim = VatRegime(**podatki_rezima(nacin_obracuna="NEVELJAVNO"))

    with pytest.raises(ValidationError):
        rezim.full_clean()


def test_decimal_polji_uporabljata_natancnost_iz_fsd():
    stopnja = VatRegime._meta.get_field("stopnja")
    cena = ServiceCatalogItem._meta.get_field("privzeta_cena")

    assert (stopnja.max_digits, stopnja.decimal_places) == (5, 2)
    assert (cena.max_digits, cena.decimal_places) == (10, 2)


@pytest.mark.django_db
def test_sifra_rezima_je_enolicna():
    VatRegime.objects.create(**podatki_rezima())

    with pytest.raises(IntegrityError), transaction.atomic():
        VatRegime.objects.create(**podatki_rezima())


@pytest.mark.django_db
def test_service_catalog_item_dovoljuje_prazne_privzete_vrednosti():
    postavka = ServiceCatalogItem.objects.create(
        naziv="Svetovanje",
        enota_mere="ura",
    )

    assert postavka.privzeta_cena is None
    assert postavka.privzeti_ddv_rezim is None
    assert postavka.aktivna is True


@pytest.mark.django_db
def test_uporabljenega_privzetega_ddv_rezima_ni_mogoce_izbrisati():
    rezim = VatRegime.objects.create(**podatki_rezima())
    ServiceCatalogItem.objects.create(
        naziv="Najemnina",
        privzeti_ddv_rezim=rezim,
        enota_mere="mesec",
    )

    with pytest.raises(ProtectedError):
        rezim.delete()
