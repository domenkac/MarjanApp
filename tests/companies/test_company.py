import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction

from companies.models import Company


def podatki_podjetja(**spremembe):
    podatki = {
        "naziv": "Alfa d.o.o.",
        "davcna_stevilka": "15012557",
        "maticna_stevilka": "1234567000",
        "naslov": "Glavna ulica 1",
        "trr": "si56 1910 0000 0123 438",
        "logotip": SimpleUploadedFile("logo.png", b"\x89PNG\r\n\x1a\n"),
        "email_racunovodja": "racunovodja@example.com",
        "email_posiljatelj": "racuni@example.com",
        "keychain_kljuc_posiljatelja": "marjanapp-alfa",
    }
    return podatki | spremembe


@pytest.mark.django_db
def test_veljavno_podjetje_in_privzete_vrednosti():
    company = Company(**podatki_podjetja())

    company.full_clean()

    assert company.trr == "SI56191000000123438"
    assert company.ddv_zavezanec is True
    assert company.privzeti_dostavni_kanal == Company.DostavniKanal.PDF
    assert company.rok_placila_dni_privzeto == 15
    assert company.ddv_razporeditev_potrjena is False
    assert company.aktivno is True


@pytest.mark.django_db
def test_veljaven_tuji_iban_je_dovoljen():
    company = Company(**podatki_podjetja(trr="DE89 3704 0044 0532 0130 00"))

    company.full_clean()

    assert company.trr == "DE89370400440532013000"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("polje", "vrednost", "sporocilo"),
    [
        ("davcna_stevilka", "15012558", "Neveljavna davčna številka"),
        ("maticna_stevilka", "123456789", "natanko 10 mest"),
        ("trr", "SI56191000000123439", "Neveljaven IBAN"),
        ("trr", "DE8937040044053201300", "Neveljaven IBAN"),
    ],
)
def test_neveljavni_podatki_so_zavrnjeni(polje, vrednost, sporocilo):
    company = Company(**podatki_podjetja(**{polje: vrednost}))

    with pytest.raises(ValidationError, match=sporocilo):
        company.full_clean()


@pytest.mark.django_db
def test_logotip_mora_biti_jpg_ali_png():
    company = Company(
        **podatki_podjetja(
            logotip=SimpleUploadedFile("logo.png", b"ni slika"),
        )
    )

    with pytest.raises(ValidationError, match="JPG ali PNG"):
        company.full_clean()


@pytest.mark.django_db
def test_clean_razumljivo_zavrne_podvojeno_davcno_aktivnega_podjetja():
    Company.objects.create(**podatki_podjetja(logotip="logo.png"))
    company = Company(**podatki_podjetja())

    with pytest.raises(ValidationError) as napaka:
        company.full_clean()

    assert napaka.value.message_dict["davcna_stevilka"] == [
        "Aktivno podjetje s to davčno številko že obstaja."
    ]


@pytest.mark.django_db
def test_neaktivni_podjetji_lahko_delita_davcno_stevilko():
    Company.objects.create(**podatki_podjetja(logotip="logo.png", aktivno=False))

    Company.objects.create(**podatki_podjetja(logotip="logo.png", aktivno=False))


@pytest.mark.django_db
def test_db_omejitev_zavrne_podvojeno_davcno_aktivnega_podjetja():
    Company.objects.create(**podatki_podjetja(logotip="logo.png"))

    with pytest.raises(IntegrityError), transaction.atomic():
        Company.objects.create(**podatki_podjetja(logotip="logo.png"))
