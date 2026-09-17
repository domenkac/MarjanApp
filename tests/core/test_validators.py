import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from core.validators import (
    preveri_davcno_stevilko,
    preveri_maticno_stevilko,
    preveri_sliko,
)
from tests.image_data import slika_bytes


@pytest.mark.parametrize(
    ("stevilka", "veljavna"),
    [
        ("15012557", True),
        ("70000000", True),
        ("15012558", False),
        ("00000000", False),
        ("1501255", False),
        ("1501255A", False),
    ],
)
def test_davcna_stevilka(stevilka, veljavna):
    assert preveri_davcno_stevilko(stevilka) is veljavna


@pytest.mark.parametrize(
    ("stevilka", "veljavna"),
    [
        ("1234567000", True),
        ("123456789", False),
        ("12345678901", False),
        ("123456789A", False),
    ],
)
def test_maticna_stevilka(stevilka, veljavna):
    assert preveri_maticno_stevilko(stevilka) is veljavna

@pytest.mark.parametrize(
    ("format", "ime", "mime"),
    [("JPEG", "slika.txt", "text/plain"), ("PNG", "slika.jpg", "image/jpeg")],
)
def test_slika_je_veljavna_po_vsebini_in_polozaj_ostane(format, ime, mime):
    datoteka = SimpleUploadedFile(ime, slika_bytes(format), content_type=mime)
    datoteka.seek(2)

    preveri_sliko(datoteka)

    assert datoteka.tell() == 2


@pytest.mark.parametrize(
    ("ime", "vsebina"),
    [
        ("slika.jpg", b"navadno besedilo"),
        ("slika.png", b"navadno besedilo"),
        ("slika.jpg", b"\xff\xd8\xff"),
        ("slika.png", b"\x89PNG\r\n\x1a\n"),
    ],
)
def test_fake_slika_je_zavrnjena_in_polozaj_ostane(ime, vsebina):
    datoteka = SimpleUploadedFile(ime, vsebina)
    datoteka.seek(2)

    with pytest.raises(ValidationError, match="JPG ali PNG"):
        preveri_sliko(datoteka)

    assert datoteka.tell() == 2


def test_slika_nad_pet_mib_je_zavrnjena():
    datoteka = SimpleUploadedFile("velika.png", slika_bytes() + b"x" * (5 * 1024 * 1024))

    with pytest.raises(ValidationError, match="5 MiB"):
        preveri_sliko(datoteka)


def test_odrezana_slika_je_zavrnjena():
    datoteka = SimpleUploadedFile("odrezana.jpg", slika_bytes("JPEG")[:30])

    with pytest.raises(ValidationError, match="JPG ali PNG"):
        preveri_sliko(datoteka)


def test_absurdne_dimenzije_so_zavrnjene():
    datoteka = SimpleUploadedFile("velika.png", slika_bytes(size=(5000, 5000)))

    with pytest.raises(ValidationError, match="dimenzije"):
        preveri_sliko(datoteka)


def test_pillow_decompression_bomb_je_zavrnjena(monkeypatch):
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1)
    datoteka = SimpleUploadedFile("bomba.png", slika_bytes())

    with pytest.raises(ValidationError, match="JPG ali PNG"):
        preveri_sliko(datoteka)
