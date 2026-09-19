import base64
import hashlib
import mimetypes
import os
from dataclasses import dataclass
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import unquote_to_bytes, urlsplit

if os.name == "nt":
    _msys_bin = Path(os.environ.get("MSYS2_ROOT", r"C:\msys64")) / "mingw64" / "bin"
    if _msys_bin.is_dir():
        os.add_dll_directory(str(_msys_bin))

from django.core.files.storage import default_storage
from django.template import Context, Engine
from weasyprint import HTML
from weasyprint.urls import URLFetcherResponse

from catalogs.models import VatRegime
from core.pravila import slugify_naziv
from invoicing.models import Invoice
from invoicing.rendering import nastavi_pdf_artifact

from .contexts import (
    PdfContext,
    PdfRenderNapaka,
    pripravi_pdf_context,
    pripravi_vzorcni_pdf_context,
)
from .paths import NeveljavnaRenderPot, varna_render_pot


@dataclass(frozen=True, slots=True)
class PdfRenderRezultat:
    pot: str
    checksum: str
    pdf_bytes: bytes


@dataclass(frozen=True, slots=True)
class DdvRekapitulacija:
    oznaka: str
    osnova: Decimal
    ddv: Decimal


_PREDLOGA = Path(__file__).parent / "templates" / "rendering" / "racun.html"
_ENGINE = Engine(dirs=[str(_PREDLOGA.parent)])


def _znesek(vrednost: Decimal) -> str:
    return f"{vrednost:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _datum(vrednost) -> str:
    return vrednost.strftime("%d.%m.%Y")


def _data_uri(pot: str) -> str:
    try:
        with default_storage.open(pot, "rb") as datoteka:
            vsebina = datoteka.read()
    except OSError as napaka:
        raise PdfRenderNapaka("ASSET_MANJKA", f"Slika ne obstaja: {pot}.") from napaka
    mime, _ = mimetypes.guess_type(pot)
    if vsebina.startswith(bytes.fromhex("ffd8ff")):
        mime = "image/jpeg"
    elif vsebina.startswith(bytes.fromhex("89504e470d0a1a0a")) or mime is None:
        mime = "image/png"
    if mime not in {"image/jpeg", "image/png"}:
        raise PdfRenderNapaka("ASSET_NEVELJAVEN", f"Slika ni dovoljena: {pot}.")
    return f"data:{mime};base64,{base64.b64encode(vsebina).decode()}"


def _url_fetcher(url: str):
    if urlsplit(url).scheme != "data":
        raise PdfRenderNapaka("ASSET_NEVELJAVEN", "Zunanji ali lokalni URL asseta ni dovoljen.")
    glava, vsebina = url.split(",", 1)
    mime = glava[5:].split(";")[0]
    body = base64.b64decode(vsebina) if ";base64" in glava else unquote_to_bytes(vsebina)
    return URLFetcherResponse(url, body=BytesIO(body), headers={"Content-Type": mime})


def sestavi_ddv_rekapitulacijo(context: PdfContext) -> tuple[DdvRekapitulacija, ...]:
    skupine = {}
    ni_predmet_osnova = Decimal("0.00")
    for vrstica in context.vrstice:
        if vrstica.ddv_nacin_obracuna == VatRegime.NacinObracuna.NI_PREDMET:
            ni_predmet_osnova += vrstica.osnova
            continue
        kljuc = (vrstica.ddv_nacin_obracuna, vrstica.ddv_rekap_skupina, vrstica.ddv_stopnja)
        osnova, ddv = skupine.get(kljuc, (Decimal("0.00"), Decimal("0.00")))
        skupine[kljuc] = (osnova + vrstica.osnova, ddv + vrstica.ddv_znesek)

    rezultat = []
    for (nacin, skupina, stopnja), (osnova, ddv) in skupine.items():
        if nacin == VatRegime.NacinObracuna.OBDAVCENO:
            oznaka = f"{skupina} ({_znesek(stopnja)} %)"
        elif nacin == VatRegime.NacinObracuna.OPROSCENO:
            oznaka = f"Opro\u0161\u010deno \u2013 {skupina}"
        else:
            oznaka = f"Obrnjena dav\u010dna obveznost \u2013 {skupina}"
        rezultat.append(DdvRekapitulacija(oznaka, osnova, ddv))

    if (
        sum((v.osnova for v in rezultat), ni_predmet_osnova) != context.osnova_skupaj
        or sum((v.ddv for v in rezultat), Decimal("0.00")) != context.ddv_skupaj
    ):
        raise PdfRenderNapaka("DDV_REKAP_NESKLADNA", "DDV rekapitulacija ni skladna z vsotami.")
    return tuple(rezultat)


def _klavzule(context: PdfContext) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            vrstica.ddv_klavzula
            for vrstica in context.vrstice
            if vrstica.ddv_nacin_obracuna
            in (VatRegime.NacinObracuna.OPROSCENO, VatRegime.NacinObracuna.OBRNJENA_DAVCNA)
            and vrstica.ddv_klavzula
        )
    )


def pripravi_html(context: PdfContext) -> str:
    predloga = _ENGINE.get_template(_PREDLOGA.name)
    return predloga.render(Context({
        "context": context,
        "logo": _data_uri(context.izdajatelj_logotip_slika_pot),
        "podpis": _data_uri(context.izdajatelj_podpisnik_slika_pot),
        "datum_izdaje": _datum(context.datum_izdaje),
        "obdobje": f"{_datum(context.obdobje_od)} \u2013 {_datum(context.obdobje_do)}",
        "rok_placila": _datum(context.rok_placila) if context.rok_placila else None,
        "vrstice": tuple({"opis": v.opis, "osnova": _znesek(v.osnova), "ddv_stopnja": _znesek(v.ddv_stopnja), "ddv_znesek": _znesek(v.ddv_znesek), "skupaj": _znesek(v.skupaj)} for v in context.vrstice),
        "rekapitulacija": tuple({"oznaka": v.oznaka, "osnova": _znesek(v.osnova), "ddv": _znesek(v.ddv)} for v in sestavi_ddv_rekapitulacijo(context)),
        "klavzule": _klavzule(context),
        "osnova_skupaj": _znesek(context.osnova_skupaj),
        "ddv_skupaj": _znesek(context.ddv_skupaj),
        "skupaj_z_ddv": _znesek(context.skupaj_z_ddv),
    }))


def _generiraj_pdf_bytes(context: PdfContext) -> bytes:
    try:
        pdf_bytes = HTML(string=pripravi_html(context), url_fetcher=_url_fetcher).write_pdf()
    except PdfRenderNapaka:
        raise
    except Exception as napaka:
        raise PdfRenderNapaka("PDF_GENERIRANJE_NI_USPELO", "Generiranje PDF ni uspelo.") from napaka
    if not pdf_bytes.startswith(b"%PDF-"):
        raise PdfRenderNapaka("PDF_NEVELJAVEN", "Renderer ni vrnil PDF dokumenta.")
    return pdf_bytes


def _preveri_obstojeci_artifact(invoice: Invoice) -> PdfRenderRezultat | None:
    if not invoice.rendered_pdf_path and not invoice.checksum:
        return None
    if not invoice.rendered_pdf_path or not invoice.checksum:
        raise PdfRenderNapaka("PDF_INTEGRITETA", "PDF pot ali checksum manjka.")
    try:
        pot = varna_render_pot(invoice.rendered_pdf_path)
    except NeveljavnaRenderPot as napaka:
        raise PdfRenderNapaka("PDF_INTEGRITETA", "PDF pot ni veljavna.") from napaka
    if not pot.is_file():
        raise PdfRenderNapaka("PDF_INTEGRITETA", "PDF artifact manjka.")
    pdf_bytes = pot.read_bytes()
    if hashlib.sha256(pdf_bytes).hexdigest() != invoice.checksum:
        raise PdfRenderNapaka("PDF_INTEGRITETA", "PDF checksum se ne ujema.")
    return PdfRenderRezultat(invoice.rendered_pdf_path, invoice.checksum, pdf_bytes)


def _zapisi_atomsko(pot: Path, pdf_bytes: bytes, *, zamenjaj: bool = False) -> None:
    pot.parent.mkdir(parents=True, exist_ok=True)
    if pot.exists() and not zamenjaj:
        raise PdfRenderNapaka("PDF_POT_ZASEDENA", "Ciljna PDF pot že obstaja.")
    zacasna = None
    try:
        with NamedTemporaryFile(dir=pot.parent, prefix=".render-", suffix=".tmp", delete=False) as datoteka:
            zacasna = Path(datoteka.name)
            datoteka.write(pdf_bytes)
            datoteka.flush()
            os.fsync(datoteka.fileno())
        os.replace(zacasna, pot)
    except OSError as napaka:
        raise PdfRenderNapaka("PDF_ZAPIS_NI_USPEL", "Zapis PDF ni uspel.") from napaka
    finally:
        if zacasna and zacasna.exists():
            zacasna.unlink()


def render_pdf(invoice: Invoice) -> PdfRenderRezultat:
    context = pripravi_pdf_context(invoice)
    svezi = Invoice.objects.get(pk=invoice.pk)
    obstojeci = _preveri_obstojeci_artifact(svezi)
    if obstojeci is not None:
        return obstojeci

    ime_datoteke = f"{context.stevilka}.pdf"
    try:
        pot = varna_render_pot(ime_datoteke)
    except NeveljavnaRenderPot as napaka:
        raise PdfRenderNapaka("PDF_POT_NEVELJAVNA", "PDF ime ni veljavno.") from napaka
    pdf_bytes = _generiraj_pdf_bytes(context)
    checksum = hashlib.sha256(pdf_bytes).hexdigest()
    _zapisi_atomsko(pot, pdf_bytes)
    try:
        nastavi_pdf_artifact(invoice, rendered_pdf_path=ime_datoteke, checksum=checksum)
    except Exception as napaka:
        raise PdfRenderNapaka("PDF_METAPODATKI_NISO_SHRANJENI", "PDF metapodatki niso shranjeni.") from napaka
    return PdfRenderRezultat(ime_datoteke, checksum, pdf_bytes)

def render_sample_pdf(lease, *, datum_izdaje, obdobje_od, obdobje_do) -> PdfRenderRezultat:
    context = pripravi_vzorcni_pdf_context(
        lease,
        datum_izdaje=datum_izdaje,
        obdobje_od=obdobje_od,
        obdobje_do=obdobje_do,
    )
    ime_datoteke = f"VZOREC-{slugify_naziv(context.izdajatelj_naziv)}.pdf"
    try:
        pot = varna_render_pot(ime_datoteke)
    except NeveljavnaRenderPot as napaka:
        raise PdfRenderNapaka("PDF_POT_NEVELJAVNA", "PDF ime ni veljavno.") from napaka
    pdf_bytes = _generiraj_pdf_bytes(context)
    rezultat = PdfRenderRezultat(ime_datoteke, hashlib.sha256(pdf_bytes).hexdigest(), pdf_bytes)
    _zapisi_atomsko(pot, pdf_bytes, zamenjaj=True)
    return rezultat
