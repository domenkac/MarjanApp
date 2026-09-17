from datetime import date

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction

from companies.models import Company, Signatory
from tests.image_data import slika_bytes


def ustvari_podjetje(naziv="Alfa d.o.o.", davcna_stevilka="15012557"):
    return Company.objects.create(
        naziv=naziv,
        davcna_stevilka=davcna_stevilka,
        maticna_stevilka="1234567000",
        naslov="Glavna ulica 1",
        trr="SI56191000000123438",
        logotip="logo.png",
        email_racunovodja="racunovodja@example.com",
        email_posiljatelj="racuni@example.com",
        keychain_kljuc_posiljatelja="marjanapp-alfa",
    )


def podatki_podpisnika(company, **spremembe):
    podatki = {
        "company": company,
        "ime_priimek": "Ana Novak",
        "podpis_slika": "podpis.png",
        "velja_od": date(2026, 1, 1),
    }
    return podatki | spremembe


@pytest.mark.django_db
def test_en_trenutni_podpisnik_je_dovoljen():
    company = ustvari_podjetje()

    Signatory.objects.create(**podatki_podpisnika(company))


@pytest.mark.django_db
def test_vec_zgodovinskih_podpisnikov_je_dovoljenih():
    company = ustvari_podjetje()

    Signatory.objects.create(
        **podatki_podpisnika(company, velja_do=date(2024, 12, 31))
    )
    Signatory.objects.create(
        **podatki_podpisnika(
            company,
            ime_priimek="Boris Kralj",
            velja_od=date(2025, 1, 1),
            velja_do=date(2025, 12, 31),
        )
    )


@pytest.mark.django_db
def test_db_zavrne_drugega_trenutnega_podpisnika_istega_podjetja():
    company = ustvari_podjetje()
    Signatory.objects.create(**podatki_podpisnika(company))

    with pytest.raises(IntegrityError), transaction.atomic():
        Signatory.objects.create(
            **podatki_podpisnika(company, ime_priimek="Boris Kralj")
        )


@pytest.mark.django_db
def test_drugo_podjetje_ima_lahko_svojega_trenutnega_podpisnika():
    prvo = ustvari_podjetje()
    drugo = ustvari_podjetje("Beta d.o.o.", "25012550")

    Signatory.objects.create(**podatki_podpisnika(prvo))
    Signatory.objects.create(**podatki_podpisnika(drugo))


@pytest.mark.django_db
def test_clean_razumljivo_zavrne_drugega_trenutnega_podpisnika():
    company = ustvari_podjetje()
    Signatory.objects.create(**podatki_podpisnika(company))
    drugi = Signatory(
        **podatki_podpisnika(
            company,
            ime_priimek="Boris Kralj",
            podpis_slika=SimpleUploadedFile("podpis.png", slika_bytes()),
        )
    )

    with pytest.raises(ValidationError) as napaka:
        drugi.full_clean()

    assert napaka.value.message_dict["velja_do"] == [
        "Podjetje že ima trenutnega podpisnika."
    ]


@pytest.mark.django_db
def test_podpis_mora_biti_jpg_ali_png():
    podpisnik = Signatory(
        **podatki_podpisnika(
            ustvari_podjetje(),
            podpis_slika=SimpleUploadedFile("podpis.png", b"ni slika"),
        )
    )

    with pytest.raises(ValidationError, match="JPG ali PNG"):
        podpisnik.full_clean()
