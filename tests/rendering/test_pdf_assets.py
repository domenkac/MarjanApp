import base64
from dataclasses import replace

import pytest
from django.core.files.base import ContentFile

from companies.models import Signatory
from core.services import record_edit_fingerprint
from invoicing.models import Invoice
from rendering.contexts import pripravi_pdf_context
from rendering.services import _generiraj_pdf_bytes, pripravi_html, render_pdf
from tests.rendering.test_contexts import (
    VALID_PNG_LOGO,
    VALID_PNG_NOV_ASSET,
    VALID_PNG_PODPIS,
)
from tests.rendering.test_contexts import (
    izdani_racun as source_izdani_racun,  # noqa: F401
)


@pytest.fixture(name="izdani_racun")
def fixture_izdani_racun(request, db):
    return request.getfixturevalue("source_izdani_racun")


def test_html_ddv_rekapitulacija_nima_dvojnega_odstotka(izdani_racun):
    html = pripravi_html(pripravi_pdf_context(izdani_racun))

    assert "DDV 22 % (22,00 %)" in html
    assert "%%" not in html

def test_html_in_pdf_ohrani_sumnike(izdani_racun):
    znaki = "\u010d \u010c \u0161 \u0160 \u017e \u017d"
    context = pripravi_pdf_context(izdani_racun)
    context = replace(
        context,
        prejemnik_naziv=znaki,
        izdajatelj_podpisnik_ime=znaki,
        vrstice=(replace(context.vrstice[0], opis=znaki),),
    )

    html = pripravi_html(context)
    pdf_bytes = _generiraj_pdf_bytes(context)

    assert html.count(znaki) == 3
    assert pdf_bytes.startswith(b"%PDF-")




def test_renderer_vgradi_veljavna_snapshot_logo_in_podpis(
    izdani_racun, settings, tmp_path
):
    settings.RENDERING_ROOT = tmp_path / "rendered"
    invoice = Invoice.objects.get(pk=izdani_racun.pk)
    company = invoice.company
    podpisnik = Signatory.objects.get(company=company)
    company.logotip.save("nov-logo.png", ContentFile(VALID_PNG_NOV_ASSET), save=True)
    podpisnik._business_edit = (record_edit_fingerprint(podpisnik), "Nov podpis")
    podpisnik.podpis_slika.save(
        "nov-podpis.png", ContentFile(VALID_PNG_NOV_ASSET), save=True
    )

    html = pripravi_html(pripravi_pdf_context(invoice))
    rezultat = render_pdf(invoice)

    assert base64.b64encode(VALID_PNG_LOGO).decode() in html
    assert base64.b64encode(VALID_PNG_PODPIS).decode() in html
    assert base64.b64encode(VALID_PNG_NOV_ASSET).decode() not in html
    assert rezultat.pdf_bytes.startswith(b"%PDF-")
    assert b"/Image" in rezultat.pdf_bytes
    assert b"Logotip izdajatelja" not in rezultat.pdf_bytes
    assert b"Podpis" not in rezultat.pdf_bytes
