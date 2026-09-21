from hashlib import sha256

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from companies.services import shrani_nespremenljiv_asset


@pytest.mark.django_db
def test_socasen_zapis_istega_asseta_uporabi_kanonicno_pot(
    settings, tmp_path, monkeypatch
):
    settings.MEDIA_ROOT = tmp_path
    vsebina = b"isti asset"
    (tmp_path / "vir.png").write_bytes(vsebina)
    cilj = f"nespremenljivi-asseti/{sha256(vsebina).hexdigest()}"
    pravi_save = default_storage.save
    ustvarjena_alternativa = None

    def socasen_save(ime, datoteka):
        nonlocal ustvarjena_alternativa
        podatki = datoteka.read()
        assert pravi_save(ime, ContentFile(podatki)) == cilj
        ustvarjena_alternativa = pravi_save(ime, ContentFile(podatki))
        return ustvarjena_alternativa

    monkeypatch.setattr(default_storage, "save", socasen_save)

    assert shrani_nespremenljiv_asset("vir.png") == cilj
    assert (tmp_path / cilj).read_bytes() == vsebina
    assert ustvarjena_alternativa != cilj
    assert not (tmp_path / ustvarjena_alternativa).exists()
