from pathlib import Path

import pytest

from rendering.paths import (
    NeveljavnaRenderPot,
    varna_render_pot,
    varna_render_pot_podjetja,
)


def test_varna_render_pot_ostane_v_render_rootu(settings, tmp_path):
    settings.RENDERING_ROOT = tmp_path / "render"
    assert varna_render_pot("NP-00001.pdf") == (
        settings.RENDERING_ROOT / "NP-00001.pdf"
    ).resolve()


@pytest.mark.parametrize(
    "ime",
    [
        "..",
        "../racun.pdf",
        ".." + chr(92) + "racun.pdf",
        "mapa/racun.pdf",
        "mapa" + chr(92) + "racun.pdf",
        str(Path("C:/izven/racun.pdf").resolve()),
    ],
)
def test_nevarna_render_pot_je_zavrnjena(settings, tmp_path, ime):
    settings.RENDERING_ROOT = tmp_path / "render"
    with pytest.raises(NeveljavnaRenderPot):
        varna_render_pot(ime)


def test_varna_render_pot_podjetja_dovoli_samo_interni_namespace(
    settings, tmp_path
):
    settings.RENDERING_ROOT = tmp_path / "render"

    relativna, cilj = varna_render_pot_podjetja(12, "NP-00001.xml")

    assert relativna == "company-12/NP-00001.xml"
    assert cilj == (settings.RENDERING_ROOT / relativna).resolve()


@pytest.mark.parametrize(
    ("company_id", "ime"),
    [
        (0, "NP-00001.xml"),
        (-1, "NP-00001.xml"),
        (True, "NP-00001.xml"),
        ("1", "NP-00001.xml"),
        (1, "../NP-00001.xml"),
        (1, "mapa/NP-00001.xml"),
        (1, str(Path("C:/izven/NP-00001.xml").resolve())),
    ],
)
def test_nevarna_render_pot_podjetja_je_zavrnjena(
    settings, tmp_path, company_id, ime
):
    settings.RENDERING_ROOT = tmp_path / "render"
    with pytest.raises(NeveljavnaRenderPot):
        varna_render_pot_podjetja(company_id, ime)
