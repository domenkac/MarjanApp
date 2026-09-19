from pathlib import Path

import pytest

from rendering.paths import NeveljavnaRenderPot, varna_render_pot


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