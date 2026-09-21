from hashlib import sha256
from pathlib import Path

import pytest

from invoicing.models import Invoice
from rendering.contexts import EslogRenderNapaka
from rendering.services import render_eslog

pytest_plugins = ["tests.rendering.test_contexts"]


def test_eslog_shrani_bytes_checksum_in_metapodatke(
    izdani_racun, settings, tmp_path
):
    settings.RENDERING_ROOT = tmp_path / "rendered"

    rezultat = render_eslog(izdani_racun)
    shranjeni_bytes = (settings.RENDERING_ROOT / rezultat.pot).read_bytes()

    assert rezultat.pot == f"company-{izdani_racun.company_id}/NP-00001.xml"
    assert shranjeni_bytes == rezultat.xml_bytes
    assert rezultat.checksum == sha256(shranjeni_bytes).hexdigest()
    izdani_racun.refresh_from_db()
    assert (
        izdani_racun.rendered_eslog_path,
        izdani_racun.rendered_eslog_checksum,
    ) == (rezultat.pot, rezultat.checksum)


def test_ponovni_eslog_render_vrne_isti_artifact(izdani_racun, settings, tmp_path):
    settings.RENDERING_ROOT = tmp_path / "rendered"

    prvi = render_eslog(izdani_racun)
    drugi = render_eslog(izdani_racun)

    assert drugi == prvi


def test_ponovni_eslog_render_ne_generira_xml(
    izdani_racun, settings, tmp_path, monkeypatch
):
    settings.RENDERING_ROOT = tmp_path / "rendered"
    prvi = render_eslog(izdani_racun)

    def napaka(*args, **kwargs):
        raise AssertionError("XML se ne sme ponovno generirati.")

    monkeypatch.setattr("rendering.services.generiraj_eslog", napaka)

    assert render_eslog(izdani_racun) == prvi


def test_neskladni_eslog_metapodatki_so_zavrnjeni(izdani_racun, settings, tmp_path):
    settings.RENDERING_ROOT = tmp_path / "rendered"
    Invoice.objects.filter(pk=izdani_racun.pk).update(
        rendered_eslog_path=(
            f"company-{izdani_racun.company_id}/NP-00001.xml"
        ),
        rendered_eslog_checksum="drug-checksum",
    )

    with pytest.raises(EslogRenderNapaka) as error:
        render_eslog(izdani_racun)

    assert error.value.koda == "ESLOG_INTEGRITETA"
    izdani_racun.refresh_from_db()
    assert izdani_racun.rendered_eslog_checksum == "drug-checksum"


@pytest.mark.parametrize("napaka", ["manjka", "spremenjen"])
def test_ponovni_eslog_render_ob_neveljavnem_artifactu_zavrne_brez_prepisa(
    izdani_racun, settings, tmp_path, napaka
):
    settings.RENDERING_ROOT = tmp_path / "rendered"
    prvi = render_eslog(izdani_racun)
    pot = settings.RENDERING_ROOT / prvi.pot
    if napaka == "manjka":
        pot.unlink()
    else:
        pot.write_bytes(b"spremenjeno")

    with pytest.raises(EslogRenderNapaka) as error:
        render_eslog(izdani_racun)

    assert error.value.koda == "ESLOG_INTEGRITETA"
    izdani_racun.refresh_from_db()
    assert izdani_racun.rendered_eslog_checksum == prvi.checksum
    if napaka == "spremenjen":
        assert pot.read_bytes() == b"spremenjeno"


def test_neuspel_eslog_zapis_ne_nastavi_metapodatkov(
    izdani_racun, settings, tmp_path, monkeypatch
):
    settings.RENDERING_ROOT = tmp_path / "rendered"

    def napaka(*args, **kwargs):
        raise OSError("napaka")

    monkeypatch.setattr("rendering.services.os.link", napaka)
    with pytest.raises(EslogRenderNapaka) as error:
        render_eslog(izdani_racun)

    assert error.value.koda == "ESLOG_ZAPIS_NI_USPEL"
    izdani_racun.refresh_from_db()
    assert not izdani_racun.rendered_eslog_path
    assert not izdani_racun.rendered_eslog_checksum
    assert not (
        settings.RENDERING_ROOT
        / f"company-{izdani_racun.company_id}"
        / "NP-00001.xml"
    ).exists()


def test_neuspela_eslog_validacija_ne_nastavi_metapodatkov(
    izdani_racun, settings, tmp_path, monkeypatch
):
    settings.RENDERING_ROOT = tmp_path / "rendered"

    def napaka(invoice):
        raise EslogRenderNapaka("ESLOG_XSD_NEVELJAVEN", "Neveljaven XML.")

    monkeypatch.setattr("rendering.services.generiraj_eslog", napaka)
    with pytest.raises(EslogRenderNapaka) as error:
        render_eslog(izdani_racun)

    assert error.value.koda == "ESLOG_XSD_NEVELJAVEN"
    izdani_racun.refresh_from_db()
    assert not izdani_racun.rendered_eslog_path
    assert not izdani_racun.rendered_eslog_checksum
    assert not settings.RENDERING_ROOT.exists()


def test_neuspeli_eslog_metapodatki_odstranijo_artifact_in_dovolijo_ponovitev(
    izdani_racun, settings, tmp_path, monkeypatch
):
    settings.RENDERING_ROOT = tmp_path / "rendered"

    def napaka(*args, **kwargs):
        raise ValueError("napaka")

    with monkeypatch.context() as patch:
        patch.setattr("rendering.services.nastavi_eslog_artifact", napaka)
        with pytest.raises(EslogRenderNapaka) as error:
            render_eslog(izdani_racun)

    assert error.value.koda == "ESLOG_METAPODATKI_NISO_SHRANJENI"
    assert not (
        settings.RENDERING_ROOT
        / f"company-{izdani_racun.company_id}"
        / "NP-00001.xml"
    ).exists()
    izdani_racun.refresh_from_db()
    assert not izdani_racun.rendered_eslog_path
    assert not izdani_racun.rendered_eslog_checksum

    assert render_eslog(izdani_racun).pot == (
        f"company-{izdani_racun.company_id}/NP-00001.xml"
    )


def test_eslog_metapodatki_ne_smejo_kazati_na_drug_veljaven_artifact(
    izdani_racun, settings, tmp_path
):
    settings.RENDERING_ROOT = tmp_path / "rendered"
    prvi = render_eslog(izdani_racun)
    druga_pot = settings.RENDERING_ROOT / f"company-{izdani_racun.company_id}" / "DRUG-RACUN.xml"
    druga_pot.parent.mkdir(parents=True, exist_ok=True)
    druga_pot.write_bytes(prvi.xml_bytes)
    Invoice.objects.filter(pk=izdani_racun.pk).update(
        rendered_eslog_path=(
            f"company-{izdani_racun.company_id}/{druga_pot.name}"
        ),
        rendered_eslog_checksum=sha256(prvi.xml_bytes).hexdigest(),
    )

    with pytest.raises(EslogRenderNapaka) as error:
        render_eslog(izdani_racun)

    assert error.value.koda == "ESLOG_INTEGRITETA"
    assert druga_pot.read_bytes() == prvi.xml_bytes


def test_eslog_atomski_zapis_ne_prepise_obstojece_datoteke(
    izdani_racun, settings, tmp_path
):
    settings.RENDERING_ROOT = tmp_path / "rendered"
    pot = (
        settings.RENDERING_ROOT
        / f"company-{izdani_racun.company_id}"
        / "NP-00001.xml"
    )
    pot.parent.mkdir(parents=True)
    pot.write_bytes(b"tuj artifact")

    with pytest.raises(EslogRenderNapaka) as error:
        render_eslog(izdani_racun)

    assert error.value.koda == "ESLOG_POT_ZASEDENA"
    assert pot.read_bytes() == b"tuj artifact"


def test_eslog_uporabi_varno_izhodno_pot(izdani_racun, settings, tmp_path):
    settings.RENDERING_ROOT = tmp_path / "rendered"
    Invoice.objects.filter(pk=izdani_racun.pk).update(stevilka="../racun")

    with pytest.raises(EslogRenderNapaka) as error:
        render_eslog(izdani_racun)

    assert error.value.koda == "ESLOG_POT_NEVELJAVNA"
    izdani_racun.refresh_from_db()
    assert not izdani_racun.rendered_eslog_path
    assert not izdani_racun.rendered_eslog_checksum
    assert not (tmp_path / "racun.xml").exists()


def test_eslog_cleanup_failure_je_izrecna(
    izdani_racun, settings, tmp_path, monkeypatch
):
    settings.RENDERING_ROOT = tmp_path / "rendered"
    cilj = (
        settings.RENDERING_ROOT
        / f"company-{izdani_racun.company_id}"
        / "NP-00001.xml"
    ).resolve()
    pravi_unlink = Path.unlink

    def unlink(pot, *args, **kwargs):
        if pot.resolve() == cilj:
            raise OSError("cleanup napaka")
        return pravi_unlink(pot, *args, **kwargs)

    monkeypatch.setattr(
        "rendering.services.nastavi_eslog_artifact",
        lambda *a, **k: (_ for _ in ()).throw(ValueError("napaka")),
    )
    monkeypatch.setattr(Path, "unlink", unlink)

    with pytest.raises(EslogRenderNapaka) as error:
        render_eslog(izdani_racun)

    assert error.value.koda == "ESLOG_CISCENJE_NI_USPELO"


def test_eslog_sprejme_metapodatke_ki_jih_je_shranil_drug_render(
    izdani_racun, settings, tmp_path, monkeypatch
):
    settings.RENDERING_ROOT = tmp_path / "rendered"
    prava_storitev = __import__(
        "rendering.services", fromlist=["nastavi_eslog_artifact"]
    ).nastavi_eslog_artifact

    def shrani_in_javi_napako(*args, **kwargs):
        prava_storitev(*args, **kwargs)
        raise ValueError("sočasni render")

    monkeypatch.setattr(
        "rendering.services.nastavi_eslog_artifact", shrani_in_javi_napako
    )

    rezultat = render_eslog(izdani_racun)

    izdani_racun.refresh_from_db()
    assert izdani_racun.rendered_eslog_path == rezultat.pot
    assert izdani_racun.rendered_eslog_checksum == rezultat.checksum
