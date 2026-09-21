from copy import copy
from pathlib import Path

import pytest

from invoicing.models import Invoice, InvoiceLine
from rendering.contexts import EslogRenderNapaka, PdfRenderNapaka
from rendering.services import render_eslog, render_pdf

pytest_plugins = ["tests.rendering.test_contexts"]


def _racun_drugega_podjetja(izdani_racun):
    podjetje = copy(izdani_racun.company)
    podjetje.pk = None
    podjetje.naziv = "Beta d.o.o."
    podjetje.aktivno = False
    podjetje.save()

    racun = copy(Invoice.objects.get(pk=izdani_racun.pk))
    racun.pk = None
    racun.company = podjetje
    racun.rendered_pdf_path = ""
    racun.rendered_eslog_path = ""
    racun.checksum = ""
    racun.rendered_eslog_checksum = ""
    racun.save()

    for izvorna in InvoiceLine.objects.filter(invoice=izdani_racun):
        vrstica = copy(izvorna)
        vrstica.pk = None
        vrstica.invoice = racun
        vrstica.save()
    return racun


def test_enaka_stevilka_dveh_podjetij_ne_povzroci_trka_artifactov(
    izdani_racun, settings, tmp_path
):
    settings.RENDERING_ROOT = tmp_path / "rendered"
    drugi = _racun_drugega_podjetja(izdani_racun)

    pdf_prvi = render_pdf(izdani_racun)
    pdf_drugi = render_pdf(drugi)
    xml_prvi = render_eslog(izdani_racun)
    xml_drugi = render_eslog(drugi)

    assert izdani_racun.stevilka == drugi.stevilka == "NP-00001"
    assert pdf_prvi.pot != pdf_drugi.pot
    assert xml_prvi.pot != xml_drugi.pot
    assert Path(pdf_prvi.pot).name == Path(pdf_drugi.pot).name == "NP-00001.pdf"
    assert Path(xml_prvi.pot).name == Path(xml_drugi.pot).name == "NP-00001.xml"
    assert pdf_prvi.pot.startswith(f"company-{izdani_racun.company_id}/")
    assert pdf_drugi.pot.startswith(f"company-{drugi.company_id}/")
    assert xml_prvi.pot.startswith(f"company-{izdani_racun.company_id}/")
    assert xml_drugi.pot.startswith(f"company-{drugi.company_id}/")
    for rezultat in (pdf_prvi, pdf_drugi, xml_prvi, xml_drugi):
        assert (settings.RENDERING_ROOT / rezultat.pot).is_file()

    izdani_racun.refresh_from_db()
    drugi.refresh_from_db()
    assert izdani_racun.rendered_pdf_path == pdf_prvi.pot
    assert drugi.rendered_pdf_path == pdf_drugi.pot
    assert izdani_racun.rendered_eslog_path == xml_prvi.pot
    assert drugi.rendered_eslog_path == xml_drugi.pot
    assert render_pdf(izdani_racun) == pdf_prvi
    assert render_pdf(drugi) == pdf_drugi
    assert render_eslog(izdani_racun) == xml_prvi
    assert render_eslog(drugi) == xml_drugi


def test_artifact_prvega_podjetja_ne_velja_za_drugo_podjetje(
    izdani_racun, settings, tmp_path
):
    settings.RENDERING_ROOT = tmp_path / "rendered"
    drugi = _racun_drugega_podjetja(izdani_racun)
    pdf_prvi = render_pdf(izdani_racun)
    xml_prvi = render_eslog(izdani_racun)
    Invoice.objects.filter(pk=drugi.pk).update(
        rendered_pdf_path=pdf_prvi.pot,
        checksum=pdf_prvi.checksum,
        rendered_eslog_path=xml_prvi.pot,
        rendered_eslog_checksum=xml_prvi.checksum,
    )

    with pytest.raises(PdfRenderNapaka) as pdf_napaka:
        render_pdf(drugi)
    with pytest.raises(EslogRenderNapaka) as xml_napaka:
        render_eslog(drugi)

    assert pdf_napaka.value.koda == "PDF_INTEGRITETA"
    assert xml_napaka.value.koda == "ESLOG_INTEGRITETA"
