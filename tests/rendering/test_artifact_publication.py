from concurrent.futures import ThreadPoolExecutor

from rendering.contexts import PdfRenderNapaka
from rendering.services import _objavi_atomsko, _zakleni_artifact


def _objavi(pot, vsebina):
    with _zakleni_artifact(pot):
        return _objavi_atomsko(
            pot, vsebina, tip="PDF", napaka_razred=PdfRenderNapaka
        )


def test_socasen_zapis_enakih_bytes_varno_uporabi_isti_artifact(tmp_path):
    pot = tmp_path / "artifact.pdf"

    with ThreadPoolExecutor(max_workers=2) as izvajalnik:
        rezultata = list(izvajalnik.map(lambda _: _objavi(pot, b"isti"), range(2)))

    assert sorted(rezultata) == [False, True]
    assert pot.read_bytes() == b"isti"
    assert not list(tmp_path.glob("*.tmp"))


def test_socasen_zapis_razlicnih_bytes_ne_prepise_artifacta(tmp_path):
    pot = tmp_path / "artifact.pdf"

    with ThreadPoolExecutor(max_workers=2) as izvajalnik:
        rezultata = [
            izvajalnik.submit(_objavi, pot, vsebina)
            for vsebina in (b"prvi", b"drugi")
        ]

    napake = []
    for rezultat in rezultata:
        try:
            rezultat.result()
        except PdfRenderNapaka as napaka:
            napake.append(napaka)
    assert len(napake) == 1
    assert napake[0].koda == "PDF_POT_ZASEDENA"
    assert pot.read_bytes() in {b"prvi", b"drugi"}
    assert not list(tmp_path.glob("*.tmp"))
