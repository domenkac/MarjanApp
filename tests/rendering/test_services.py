# ruff: noqa: F401, F811
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pytest

from catalogs.models import VatRegime
from companies.models import Signatory
from contracts.models import LeaseItem
from invoicing.models import Invoice
from invoicing.services import (
    dodaj_vrstico,
    potrdi_korekcijski_osnutek,
    pripravi_korekcijski_osnutek,
)
from rendering.contexts import PdfRenderNapaka, PdfVrstica, pripravi_pdf_context
from rendering.services import (
    _generiraj_pdf_bytes,
    _url_fetcher,
    pripravi_html,
    render_pdf,
    sestavi_ddv_rekapitulacijo,
)
from tests.rendering.test_contexts import izdani_racun


def test_np_pdf_zapise_bytes_checksum_in_metapodatke(izdani_racun, settings, tmp_path):
    settings.RENDERING_ROOT = tmp_path / "rendered"

    rezultat = render_pdf(izdani_racun)

    assert rezultat.pot == f"company-{izdani_racun.company_id}/NP-00001.pdf"
    assert rezultat.pdf_bytes.startswith(b"%PDF-")
    assert (settings.RENDERING_ROOT / rezultat.pot).read_bytes() == rezultat.pdf_bytes
    assert rezultat.checksum == sha256(rezultat.pdf_bytes).hexdigest()
    izdani_racun.refresh_from_db()
    assert (izdani_racun.rendered_pdf_path, izdani_racun.checksum) == (
        rezultat.pot,
        rezultat.checksum,
    )


def test_html_prikaze_snapshote_in_totale(izdani_racun):
    html = pripravi_html(pripravi_pdf_context(izdani_racun))

    for vrednost in (
        "NP",
        "NP-00001",
        "01.09.2026",
        "12/2024",
        "Stavba A",
        "Najemnina",
        "100,00",
        "122,00",
        "Ana Podpisnica",
        "Direktorica",
        "data:image/png;base64,",
    ):
        assert vrednost in html


def test_ddv_rekapitulacija_podpre_vse_rezime(izdani_racun):
    context = pripravi_pdf_context(izdani_racun)
    vrstice = (
        PdfVrstica("Obdavceno", Decimal("10.00"), Decimal("22.00"), Decimal("2.20"), Decimal("12.20"), VatRegime.NacinObracuna.OBDAVCENO, "", "DDV 22", False),
        PdfVrstica("Oprosceno", Decimal("20.00"), Decimal("0.00"), Decimal("0.00"), Decimal("20.00"), VatRegime.NacinObracuna.OPROSCENO, "Oproscena klavzula", "OPR", False),
        PdfVrstica("Ni predmet", Decimal("30.00"), Decimal("0.00"), Decimal("0.00"), Decimal("30.00"), VatRegime.NacinObracuna.NI_PREDMET, "", "NI", False),
        PdfVrstica("Obrnjena", Decimal("40.00"), Decimal("0.00"), Decimal("0.00"), Decimal("40.00"), VatRegime.NacinObracuna.OBRNJENA_DAVCNA, "Obrnjena klavzula", "OD", False),
    )
    context = replace(context, vrstice=vrstice, osnova_skupaj=Decimal("100.00"), ddv_skupaj=Decimal("2.20"), skupaj_z_ddv=Decimal("102.20"))

    rekapitulacija = sestavi_ddv_rekapitulacijo(context)
    html = pripravi_html(context)

    assert len(rekapitulacija) == 3
    assert "NI" not in [v.oznaka for v in rekapitulacija]
    assert "Oproscena klavzula" in html
    assert "Obrnjena klavzula" in html


@pytest.mark.parametrize("vir", ["tenant", "company", "vat", "lease_item", "signatory"])
def test_zivi_podatki_ne_spremenijo_html_izdanega_racuna(izdani_racun, vir):
    prej = pripravi_html(pripravi_pdf_context(izdani_racun))
    lease = izdani_racun.lease
    if vir == "tenant":
        lease.tenant.naziv = "Nov najemnik"
        lease.tenant.save(update_fields=["naziv"])
    elif vir == "company":
        izdani_racun.company.naziv = "Novo podjetje"
        izdani_racun.company.save(update_fields=["naziv"])
    elif vir == "vat":
        regime = LeaseItem.objects.get(lease=lease).vat_regime
        regime.stopnja = Decimal("9.50")
        regime.save(update_fields=["stopnja"])
    elif vir == "lease_item":
        item = LeaseItem.objects.get(lease=lease)
        item.naziv_postavke = "Nova postavka"
        item.save(update_fields=["naziv_postavke"])
    else:
        podpisnik = Signatory.objects.get(company=izdani_racun.company)
        podpisnik.ime_priimek = "Nov podpisnik"
        podpisnik.save(update_fields=["ime_priimek"])

    assert pripravi_html(pripravi_pdf_context(izdani_racun)) == prej


@pytest.mark.parametrize("tip", [Invoice.DocumentType.STORNO, Invoice.DocumentType.DOBROPIS])
def test_korekcijski_pdf_prikaze_referenco_razlog_in_brez_roka(
    izdani_racun, settings, tmp_path, tip
):
    settings.RENDERING_ROOT = tmp_path / "rendered"
    osnutek = pripravi_korekcijski_osnutek(
        izdani_racun, tip, "Popravek zneska", izdani_racun.datum_izdaje
    )
    if tip == Invoice.DocumentType.DOBROPIS:
        dodaj_vrstico(
            osnutek,
            opis="Dobropis",
            osnova=Decimal("-10.00"),
            ddv_stopnja=Decimal("0.00"),
            ddv_znesek=Decimal("0.00"),
            skupaj=Decimal("-10.00"),
            ddv_nacin_obracuna=VatRegime.NacinObracuna.NI_PREDMET,
            ddv_eslog_koda="O",
            ddv_rekap_skupina="NI",
        )
    popravek = potrdi_korekcijski_osnutek(osnutek)

    rezultat = render_pdf(popravek)
    html = pripravi_html(pripravi_pdf_context(popravek))

    assert rezultat.pdf_bytes.startswith(b"%PDF-")
    assert "Izvirni dokument: NP-00001" in html
    assert "Razlog: Popravek zneska" in html
    assert "Rok pla&#269;ila:" not in html


def test_vecstranski_context_generira_pdf(izdani_racun):
    context = pripravi_pdf_context(izdani_racun)
    vrstica = context.vrstice[0]
    vrstice = tuple(replace(vrstica, opis=f"Postavka {stevec}") for stevec in range(90))
    context = replace(
        context,
        vrstice=vrstice,
        osnova_skupaj=vrstica.osnova * 90,
        ddv_skupaj=vrstica.ddv_znesek * 90,
        skupaj_z_ddv=vrstica.skupaj * 90,
    )

    pdf_bytes = _generiraj_pdf_bytes(context)

    assert pdf_bytes.startswith(b"%PDF-")
    assert pripravi_html(context).count("Postavka") == 90


def test_neuspel_zapis_ne_nastavi_metapodatkov_ali_finalne_datoteke(
    izdani_racun, settings, tmp_path, monkeypatch
):
    settings.RENDERING_ROOT = tmp_path / "rendered"

    def napaka(*args, **kwargs):
        raise OSError("napaka")

    monkeypatch.setattr("rendering.services.os.link", napaka)
    with pytest.raises(PdfRenderNapaka) as error:
        render_pdf(izdani_racun)

    assert error.value.koda == "PDF_ZAPIS_NI_USPEL"
    izdani_racun.refresh_from_db()
    assert not izdani_racun.rendered_pdf_path
    assert not izdani_racun.checksum
    assert not (
        settings.RENDERING_ROOT
        / f"company-{izdani_racun.company_id}"
        / "NP-00001.pdf"
    ).exists()


def test_ponovni_render_vrne_isti_artifact(izdani_racun, settings, tmp_path):
    settings.RENDERING_ROOT = tmp_path / "rendered"

    prvi = render_pdf(izdani_racun)
    drugi = render_pdf(izdani_racun)

    assert drugi == prvi


def test_ponovni_pdf_render_ne_gradi_contexta(izdani_racun, settings, tmp_path, monkeypatch):
    settings.RENDERING_ROOT = tmp_path / "rendered"
    prvi = render_pdf(izdani_racun)

    def napaka(*args, **kwargs):
        raise AssertionError("Context se ne sme ponovno zgraditi.")

    monkeypatch.setattr("rendering.services.pripravi_pdf_context", napaka)

    assert render_pdf(izdani_racun) == prvi


@pytest.mark.parametrize("napaka", ["manjka", "spremenjen"])
def test_ponovni_render_ob_neveljavnem_artifactu_zavrne_brez_prepisa(
    izdani_racun, settings, tmp_path, napaka
):
    settings.RENDERING_ROOT = tmp_path / "rendered"
    prvi = render_pdf(izdani_racun)
    pot = settings.RENDERING_ROOT / prvi.pot
    if napaka == "manjka":
        pot.unlink()
    else:
        pot.write_bytes(b"spremenjeno")

    with pytest.raises(PdfRenderNapaka) as error:
        render_pdf(izdani_racun)

    assert error.value.koda == "PDF_INTEGRITETA"
    izdani_racun.refresh_from_db()
    assert izdani_racun.checksum == prvi.checksum
    if napaka == "manjka":
        assert not pot.exists()
    else:
        assert pot.read_bytes() == b"spremenjeno"


def test_neveljaven_network_asset_je_zavrnjen():
    with pytest.raises(PdfRenderNapaka) as error:
        _url_fetcher("https://primer.si/logo.png")
    assert error.value.koda == "ASSET_NEVELJAVEN"


def test_neuspeli_pdf_metapodatki_odstranijo_artifact_in_dovolijo_ponovitev(
    izdani_racun, settings, tmp_path, monkeypatch
):
    settings.RENDERING_ROOT = tmp_path / "rendered"

    def napaka(*args, **kwargs):
        raise ValueError("napaka")

    with monkeypatch.context() as patch:
        patch.setattr("rendering.services.nastavi_pdf_artifact", napaka)
        with pytest.raises(PdfRenderNapaka) as error:
            render_pdf(izdani_racun)

    assert error.value.koda == "PDF_METAPODATKI_NISO_SHRANJENI"
    izdani_racun.refresh_from_db()
    assert not izdani_racun.rendered_pdf_path
    assert not izdani_racun.checksum
    assert render_pdf(izdani_racun).pot.endswith("/NP-00001.pdf")


def test_pdf_cleanup_failure_je_izrecna(
    izdani_racun, settings, tmp_path, monkeypatch
):
    settings.RENDERING_ROOT = tmp_path / "rendered"
    cilj = (
        settings.RENDERING_ROOT
        / f"company-{izdani_racun.company_id}"
        / "NP-00001.pdf"
    ).resolve()
    pravi_unlink = Path.unlink

    def unlink(pot, *args, **kwargs):
        if pot.resolve() == cilj:
            raise OSError("cleanup napaka")
        return pravi_unlink(pot, *args, **kwargs)

    monkeypatch.setattr("rendering.services.nastavi_pdf_artifact", lambda *a, **k: (_ for _ in ()).throw(ValueError("napaka")))
    monkeypatch.setattr(Path, "unlink", unlink)

    with pytest.raises(PdfRenderNapaka) as error:
        render_pdf(izdani_racun)

    assert error.value.koda == "PDF_CISCENJE_NI_USPELO"


def test_pdf_sprejme_metapodatke_ki_jih_je_shranil_drug_render(
    izdani_racun, settings, tmp_path, monkeypatch
):
    settings.RENDERING_ROOT = tmp_path / "rendered"
    prava_storitev = __import__(
        "rendering.services", fromlist=["nastavi_pdf_artifact"]
    ).nastavi_pdf_artifact

    def shrani_in_javi_napako(*args, **kwargs):
        prava_storitev(*args, **kwargs)
        raise ValueError("sočasni render")

    monkeypatch.setattr(
        "rendering.services.nastavi_pdf_artifact", shrani_in_javi_napako
    )

    rezultat = render_pdf(izdani_racun)

    izdani_racun.refresh_from_db()
    assert izdani_racun.rendered_pdf_path == rezultat.pot
    assert izdani_racun.checksum == rezultat.checksum


def test_pdf_ne_prepise_neregistriranega_drugacnega_artifacta(
    izdani_racun, settings, tmp_path
):
    settings.RENDERING_ROOT = tmp_path / "rendered"
    pot = (
        settings.RENDERING_ROOT
        / f"company-{izdani_racun.company_id}"
        / "NP-00001.pdf"
    )
    pot.parent.mkdir(parents=True)
    pot.write_bytes(b"drug artifact")

    with pytest.raises(PdfRenderNapaka) as error:
        render_pdf(izdani_racun)

    assert error.value.koda == "PDF_POT_ZASEDENA"
    assert pot.read_bytes() == b"drug artifact"

