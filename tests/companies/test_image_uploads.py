import re

import pytest
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile

from companies.models import Company, Signatory
from core.validators import preveri_sliko
from tests.companies.test_company import podatki_podjetja
from tests.companies.test_signatory import podatki_podpisnika, ustvari_podjetje
from tests.image_data import slika_bytes


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("model", "polje", "format"),
    [(Company, "logotip", "JPEG"), (Signatory, "podpis_slika", "PNG")],
)
def test_full_clean_uporablja_skupni_validator_in_shrani_varno_ime(
    model, polje, format, settings, tmp_path
):
    settings.MEDIA_ROOT = tmp_path
    vsebina = slika_bytes(format)
    datoteka = SimpleUploadedFile("original.txt", vsebina, content_type="text/plain")
    if model is Company:
        zapis = Company(**podatki_podjetja(logotip=datoteka))
    else:
        zapis = Signatory(**podatki_podpisnika(ustvari_podjetje(), podpis_slika=datoteka))

    assert model._meta.get_field(polje).validators == [preveri_sliko]
    zapis.full_clean()
    zapis.save()

    shranjena = getattr(zapis, polje)
    assert re.fullmatch(r"slike/[0-9a-f]{32}", shranjena.name)
    assert (tmp_path / shranjena.name).read_bytes() == vsebina


@pytest.mark.django_db
@pytest.mark.parametrize("polje", ["logotip", "podpis_slika"])
@pytest.mark.parametrize("ime", ["../escape.png", "/absolute.png"])
def test_nevarno_ime_ne_ustvari_datoteke(polje, ime, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    if polje == "logotip":
        zapis = Company(**podatki_podjetja(logotip="logo.png"))
    else:
        zapis = Signatory(**podatki_podpisnika(ustvari_podjetje()))

    shranjena = getattr(zapis, polje)
    shranjena.save(ime, ContentFile(slika_bytes()), save=False)

    assert re.fullmatch(r"slike/[0-9a-f]{32}", shranjena.name)
    cilj = (tmp_path / shranjena.name).resolve()
    assert cilj.is_relative_to(tmp_path.resolve())
    assert [pot for pot in tmp_path.rglob("*") if pot.is_file()] == [cilj]
