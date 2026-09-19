from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest
from PIL import Image, ImageDraw

from catalogs.models import VatRegime
from contracts.models import LeaseItem
from invoicing.models import Invoice, InvoiceLine
from rendering.contexts import pripravi_vzorcni_pdf_context
from rendering.services import (
    _generiraj_pdf_bytes,
    pripravi_html,
    render_sample_pdf,
    sestavi_ddv_rekapitulacijo,
)
from tests.rendering.test_contexts import (
    izdani_racun as source_izdani_racun,  # noqa: F401
)


@pytest.fixture(name="izdani_racun")
def fixture_izdani_racun(request, db):
    return request.getfixturevalue("source_izdani_racun")


def test_sample_pdf_uses_production_template_without_db_side_effects(
    izdani_racun, settings, tmp_path
):
    settings.RENDERING_ROOT = tmp_path / "rendered"
    lease = izdani_racun.lease
    ni_predmet = VatRegime.objects.create(
        sifra="NI_TEST",
        naziv="Ni predmet",
        nacin_obracuna=VatRegime.NacinObracuna.NI_PREDMET,
        stopnja=Decimal("0.00"),
        rekapitulacijska_skupina="NI",
        velja_od=date(2026, 1, 1),
    )
    LeaseItem.objects.create(
        lease=lease,
        naziv_postavke="Storitev brez DDV",
        znesek_neto_mesecno=Decimal("30.00"),
        vat_regime=ni_predmet,
    )
    stanje = (Invoice.objects.count(), InvoiceLine.objects.count())

    rezultat = render_sample_pdf(
        lease,
        datum_izdaje=date(2026, 9, 1),
        obdobje_od=date(2026, 9, 1),
        obdobje_do=date(2026, 9, 30),
    )
    context = pripravi_vzorcni_pdf_context(
        lease,
        datum_izdaje=date(2026, 9, 1),
        obdobje_od=date(2026, 9, 1),
        obdobje_do=date(2026, 9, 30),
    )
    znaki = "\u010d \u010c \u0161 \u0160 \u017e \u017d"
    context = replace(
        context,
        prejemnik_naziv=znaki,
        izdajatelj_podpisnik_ime=znaki,
        vrstice=(replace(context.vrstice[0], opis=znaki), *context.vrstice[1:]),
    )
    html = pripravi_html(context)

    assert rezultat.pdf_bytes.startswith(b"%PDF-")
    assert rezultat.pot == "VZOREC-Alfa-doo.pdf"
    assert (settings.RENDERING_ROOT / rezultat.pot).is_file()
    assert context.stevilka is None
    assert "VZOREC \u2013 NI RA\u010cUN" in html
    assert "NP-00001" not in html
    assert html.count(znaki) == 3
    assert "NI" not in [v.oznaka for v in sestavi_ddv_rekapitulacijo(context)]
    assert context.skupaj_z_ddv == Decimal("152.00")
    assert (Invoice.objects.count(), InvoiceLine.objects.count()) == stanje
    assert context.pdf_template_version == settings.PDF_TEMPLATE_VERSION


def test_sample_pdf_se_lahko_varno_ponovno_generira(
    izdani_racun, settings, tmp_path
):
    settings.RENDERING_ROOT = tmp_path / "rendered"
    stanje = (Invoice.objects.count(), InvoiceLine.objects.count())
    argumenti = {
        "datum_izdaje": date(2026, 9, 1),
        "obdobje_od": date(2026, 9, 1),
        "obdobje_do": date(2026, 9, 30),
    }

    prvi = render_sample_pdf(izdani_racun.lease, **argumenti)
    drugi = render_sample_pdf(izdani_racun.lease, **argumenti)

    assert drugi == prvi
    assert (settings.RENDERING_ROOT / drugi.pot).read_bytes() == drugi.pdf_bytes
    assert (Invoice.objects.count(), InvoiceLine.objects.count()) == stanje


def test_sample_pdf_watermark_is_fixed_for_each_page(izdani_racun):
    context = pripravi_vzorcni_pdf_context(
        izdani_racun.lease,
        datum_izdaje=date(2026, 9, 1),
        obdobje_od=date(2026, 9, 1),
        obdobje_do=date(2026, 9, 30),
    )
    vrstice = tuple(
        replace(context.vrstice[0], opis=f"Vrstica {stevec}")
        for stevec in range(90)
    )
    context = replace(
        context,
        vrstice=vrstice,
        osnova_skupaj=Decimal("9000.00"),
        ddv_skupaj=Decimal("1980.00"),
        skupaj_z_ddv=Decimal("10980.00"),
    )

    html = pripravi_html(context)
    pdf_bytes = _generiraj_pdf_bytes(context)

    assert "position:fixed" in html
    assert "VZOREC \u2013 NI RA\u010cUN" in html
    assert html.count("Vrstica") == 90
    assert pdf_bytes.startswith(b"%PDF-")

def test_sample_pdf_visual_qa_uses_synthetic_images(izdani_racun, settings, tmp_path):
    settings.RENDERING_ROOT = tmp_path / "rendered"
    logo = Image.new("RGB", (300, 100), "#f8fafc")
    draw = ImageDraw.Draw(logo)
    draw.rectangle((3, 3, 296, 96), outline="#12355b", width=5)
    draw.text((105, 35), "ALFA", fill="#12355b")
    logo.save(tmp_path / "logo.png", "PNG")
    podpis = Image.new("RGBA", (300, 100), (255, 255, 255, 0))
    draw = ImageDraw.Draw(podpis)
    draw.line([(20, 70), (70, 30), (115, 70), (165, 35), (215, 70), (275, 45)], fill="#12355b", width=4)
    podpis.save(tmp_path / "podpis.png", "PNG")

    rezultat = render_sample_pdf(
        izdani_racun.lease,
        datum_izdaje=date(2026, 9, 1),
        obdobje_od=date(2026, 9, 1),
        obdobje_do=date(2026, 9, 30),
    )

    assert rezultat.pdf_bytes.startswith(b"%PDF-")
    assert b"/Image" in rezultat.pdf_bytes
