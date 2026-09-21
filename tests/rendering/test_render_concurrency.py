from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock

import pytest
from django.db import connections

from rendering.contexts import EslogRenderNapaka, PdfRenderNapaka
from rendering.services import render_eslog, render_pdf

pytest_plugins = ["tests.rendering.test_contexts"]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("tip", ["PDF", "ESLOG"])
@pytest.mark.parametrize("enaki", [True, False])
def test_socasen_render_primerja_ze_generirane_bytes(
    izdani_racun, settings, tmp_path, monkeypatch, tip, enaki
):
    settings.RENDERING_ROOT = tmp_path / "rendered"
    payloadi = (
        (b"%PDF-isti", b"%PDF-isti")
        if tip == "PDF" and enaki
        else (b"%PDF-prvi", b"%PDF-drugi")
        if tip == "PDF"
        else (b"<isti/>", b"<isti/>")
        if enaki
        else (b"<prvi/>", b"<drugi/>")
    )
    pregrada = Barrier(2)
    zaklep = Lock()
    stevec = 0

    def generiraj(*args, **kwargs):
        nonlocal stevec
        with zaklep:
            indeks = stevec
            stevec += 1
        pregrada.wait()
        return payloadi[indeks]

    if tip == "PDF":
        monkeypatch.setattr("rendering.services._generiraj_pdf_bytes", generiraj)
        render = render_pdf
        napaka_razred = PdfRenderNapaka
        koda = "PDF_SOCASNI_RENDER_NESKLADEN"
    else:
        monkeypatch.setattr("rendering.services.generiraj_eslog", generiraj)
        render = render_eslog
        napaka_razred = EslogRenderNapaka
        koda = "ESLOG_SOCASNI_RENDER_NESKLADEN"

    def izvedi():
        try:
            return render(izdani_racun)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as izvajalnik:
        rezultata = [izvajalnik.submit(izvedi) for _ in range(2)]

    uspehi = []
    napake = []
    for rezultat in rezultata:
        try:
            uspehi.append(rezultat.result())
        except napaka_razred as napaka:
            napake.append(napaka)

    if enaki:
        assert len(uspehi) == 2
        assert not napake
        assert uspehi[0] == uspehi[1]
    else:
        assert len(uspehi) == 1
        assert len(napake) == 1
        assert napake[0].koda == koda
