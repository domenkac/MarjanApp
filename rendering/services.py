import base64
import hashlib
import os
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import unquote_to_bytes, urlsplit

if os.name == "nt":
    _msys_bin = Path(os.environ.get("MSYS2_ROOT", r"C:\msys64")) / "mingw64" / "bin"
    if _msys_bin.is_dir():
        os.add_dll_directory(str(_msys_bin))

from django.conf import settings
from django.db import transaction
from django.template import Context, Engine
from weasyprint import HTML
from weasyprint.urls import URLFetcherResponse

from catalogs.models import VatRegime
from contracts.models import Lease
from core.pravila import slugify_naziv
from invoicing.models import Invoice
from invoicing.rendering import nastavi_eslog_artifact, nastavi_pdf_artifact

from .contexts import (
    EslogRenderNapaka,
    PdfContext,
    PdfRenderNapaka,
    preberi_pdf_sliko,
    pripravi_mesecni_pdf_context,
    pripravi_pdf_context,
    pripravi_vzorcni_pdf_context,
)
from .eslog import render_eslog as generiraj_eslog
from .paths import NeveljavnaRenderPot, varna_render_pot_podjetja


@dataclass(frozen=True, slots=True)
class PdfRenderRezultat:
    pot: str
    checksum: str
    pdf_bytes: bytes
    preview_id: object | None = field(default=None, compare=False)


@dataclass(frozen=True, slots=True)
class EslogRenderRezultat:
    pot: str
    checksum: str
    xml_bytes: bytes


@dataclass(frozen=True, slots=True)
class DdvRekapitulacija:
    oznaka: str
    osnova: Decimal
    ddv: Decimal


_PREDLOGA = Path(__file__).parent / "templates" / "rendering" / "racun.html"
_PREDLOGE = {"1": _PREDLOGA, "2-st": _PREDLOGA.with_name("racun_st_v2.html")}
_ENGINE = Engine(dirs=[str(_PREDLOGA.parent)])


def _znesek(vrednost: Decimal) -> str:
    return f"{vrednost:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _datum(vrednost) -> str:
    return vrednost.strftime("%d.%m.%Y")


def _data_uri(pot: str, *, nespremenljiva: bool = True) -> str:
    vsebina, mime = preberi_pdf_sliko(pot, nespremenljiva=nespremenljiva)
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
            if (context.pdf_template_version == "2-st" or vrstica.ddv_nacin_obracuna
                in (VatRegime.NacinObracuna.OPROSCENO, VatRegime.NacinObracuna.OBRNJENA_DAVCNA))
            and vrstica.ddv_klavzula
        )
    )


def pripravi_html(context: PdfContext) -> str:
    try:
        predloga_pot = _PREDLOGE[context.pdf_template_version]
    except KeyError as napaka:
        raise PdfRenderNapaka(
            "PREDLOGA_NEPODPRTA", "Shranjene različice PDF predloge ni mogoče prikazati."
        ) from napaka
    predloga = _ENGINE.get_template(predloga_pot.name)
    return predloga.render(Context({
        "context": context,
        "logo": _data_uri(context.izdajatelj_logotip_slika_pot, nespremenljiva=context.stevilka is not None),
        "podpis": _data_uri(context.izdajatelj_podpisnik_slika_pot, nespremenljiva=context.stevilka is not None),
        "datum_izdaje": _datum(context.datum_izdaje),
        "obdobje": f"{_datum(context.obdobje_od)} \u2013 {_datum(context.obdobje_do)}",
        "rok_placila": _datum(context.rok_placila) if context.rok_placila else None,
        "vrstice": tuple({
            "opis": v.opis, "osnova": _znesek(v.osnova),
            "ddv_stopnja": _znesek(v.ddv_stopnja), "ddv_znesek": _znesek(v.ddv_znesek),
            "obracunana_ddv_stopnja": (
                _znesek(v.ddv_stopnja)
                if v.ddv_nacin_obracuna == VatRegime.NacinObracuna.OBDAVCENO else "—"
            ),
            "ddv_obravnava": (
                dict(VatRegime.NacinObracuna.choices).get(v.ddv_nacin_obracuna, "")
                if v.ddv_nacin_obracuna != VatRegime.NacinObracuna.OBDAVCENO else ""
            ),
            "skupaj": _znesek(v.skupaj), "enota_mere": v.enota_mere,
            "referenca_listine": v.referenca_listine,
            "datum_reference": _datum(v.datum_reference) if v.datum_reference else None,
        } for v in context.vrstice),
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


def generiraj_mesecni_predogled_pdf(snapshot: dict, candidate: dict) -> bytes:
    return _generiraj_pdf_bytes(pripravi_mesecni_pdf_context(snapshot, candidate))


def _preveri_obstojeci_artifact(invoice: Invoice) -> PdfRenderRezultat | None:
    if not invoice.rendered_pdf_path and not invoice.checksum:
        return None
    if not invoice.rendered_pdf_path or not invoice.checksum:
        raise PdfRenderNapaka("PDF_INTEGRITETA", "PDF pot ali checksum manjka.")
    try:
        pricakovana_pot, pot = varna_render_pot_podjetja(
            invoice.company_id, f"{invoice.stevilka}.pdf"
        )
    except NeveljavnaRenderPot as napaka:
        raise PdfRenderNapaka("PDF_INTEGRITETA", "PDF pot ni veljavna.") from napaka
    if invoice.rendered_pdf_path != pricakovana_pot:
        raise PdfRenderNapaka("PDF_INTEGRITETA", "PDF pot ne ustreza računu.")
    if not pot.is_file():
        raise PdfRenderNapaka("PDF_INTEGRITETA", "PDF artifact manjka.")
    pdf_bytes = pot.read_bytes()
    if hashlib.sha256(pdf_bytes).hexdigest() != invoice.checksum:
        raise PdfRenderNapaka("PDF_INTEGRITETA", "PDF checksum se ne ujema.")
    return PdfRenderRezultat(invoice.rendered_pdf_path, invoice.checksum, pdf_bytes)


@contextmanager
def _zakleni_artifact(pot: Path):
    pot.parent.mkdir(parents=True, exist_ok=True)
    zaklepna_pot = pot.parent / f".{pot.name}.lock"
    with zaklepna_pot.open("a+b") as zaklep:
        if zaklep.seek(0, os.SEEK_END) == 0:
            zaklep.write(b"\0")
            zaklep.flush()
        zaklep.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(zaklep.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(zaklep.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            zaklep.seek(0)
            if os.name == "nt":
                msvcrt.locking(zaklep.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(zaklep.fileno(), fcntl.LOCK_UN)


def _objavi_atomsko(pot: Path, vsebina: bytes, *, tip: str, napaka_razred) -> bool:
    pot.parent.mkdir(parents=True, exist_ok=True)
    zacasna = None
    try:
        with NamedTemporaryFile(dir=pot.parent, prefix=".render-", suffix=".tmp", delete=False) as datoteka:
            zacasna = Path(datoteka.name)
            datoteka.write(vsebina)
            datoteka.flush()
            os.fsync(datoteka.fileno())
        try:
            os.link(zacasna, pot)
            return True
        except FileExistsError:
            if pot.read_bytes() == vsebina:
                return False
            raise napaka_razred(
                f"{tip}_POT_ZASEDENA", f"Ciljna {tip} pot že vsebuje drug artifact."
            )
    except (PdfRenderNapaka, EslogRenderNapaka):
        raise
    except OSError as napaka:
        raise napaka_razred(
            f"{tip}_ZAPIS_NI_USPEL", f"Zapis {tip} ni uspel."
        ) from napaka
    finally:
        if zacasna and zacasna.exists():
            try:
                zacasna.unlink()
            except OSError as napaka:
                raise napaka_razred(
                    f"{tip}_CISCENJE_NI_USPELO",
                    f"Čiščenje začasne {tip} datoteke ni uspelo.",
                ) from napaka


def _preveri_obstojeci_eslog_artifact(
    invoice: Invoice,
) -> EslogRenderRezultat | None:
    if not invoice.rendered_eslog_path and not invoice.rendered_eslog_checksum:
        return None
    if not invoice.rendered_eslog_path or not invoice.rendered_eslog_checksum:
        raise EslogRenderNapaka("ESLOG_INTEGRITETA", "eSLOG pot ali checksum manjka.")
    try:
        pricakovana_pot, pot = varna_render_pot_podjetja(
            invoice.company_id, f"{invoice.stevilka}.xml"
        )
    except NeveljavnaRenderPot as napaka:
        raise EslogRenderNapaka(
            "ESLOG_INTEGRITETA", "eSLOG pot ni veljavna."
        ) from napaka
    if invoice.rendered_eslog_path != pricakovana_pot:
        raise EslogRenderNapaka(
            "ESLOG_INTEGRITETA", "eSLOG pot ne ustreza računu."
        )
    if not pot.is_file():
        raise EslogRenderNapaka("ESLOG_INTEGRITETA", "eSLOG artifact manjka.")
    xml_bytes = pot.read_bytes()
    if hashlib.sha256(xml_bytes).hexdigest() != invoice.rendered_eslog_checksum:
        raise EslogRenderNapaka(
            "ESLOG_INTEGRITETA", "eSLOG checksum se ne ujema."
        )
    return EslogRenderRezultat(
        invoice.rendered_eslog_path, invoice.rendered_eslog_checksum, xml_bytes
    )


def _zamenjaj_atomsko(pot: Path, vsebina: bytes) -> None:
    pot.parent.mkdir(parents=True, exist_ok=True)
    zacasna = None
    try:
        with NamedTemporaryFile(
            dir=pot.parent, prefix=".render-", suffix=".tmp", delete=False
        ) as datoteka:
            zacasna = Path(datoteka.name)
            datoteka.write(vsebina)
            datoteka.flush()
            os.fsync(datoteka.fileno())
        os.replace(zacasna, pot)
    except OSError as napaka:
        raise PdfRenderNapaka("PDF_ZAPIS_NI_USPEL", "Zapis PDF ni uspel.") from napaka
    finally:
        if zacasna and zacasna.exists():
            zacasna.unlink()


def _stanje_pdf_metapodatkov(invoice: Invoice, pot: str, checksum: str) -> str:
    svezi = Invoice.objects.get(pk=invoice.pk)
    if svezi.rendered_pdf_path == pot and svezi.checksum == checksum:
        return "ISTI"
    if not svezi.rendered_pdf_path and not svezi.checksum:
        return "PRAZNI"
    return "DRUGI"


def _stanje_eslog_metapodatkov(invoice: Invoice, pot: str, checksum: str) -> str:
    svezi = Invoice.objects.get(pk=invoice.pk)
    if (
        svezi.rendered_eslog_path == pot
        and svezi.rendered_eslog_checksum == checksum
    ):
        return "ISTI"
    if not svezi.rendered_eslog_path and not svezi.rendered_eslog_checksum:
        return "PRAZNI"
    return "DRUGI"


def _odstrani_ustvarjen_artifact(
    pot: Path, vsebina: bytes, *, tip: str, napaka_razred
) -> None:
    try:
        if not pot.is_file() or pot.read_bytes() != vsebina:
            raise OSError("Artifact se je med čiščenjem spremenil.")
        pot.unlink()
    except OSError as napaka:
        raise napaka_razred(
            f"{tip}_CISCENJE_NI_USPELO",
            f"Čiščenje neuspelega {tip} artifacta ni uspelo.",
        ) from napaka


def render_pdf(invoice: Invoice) -> PdfRenderRezultat:
    svezi = Invoice.objects.filter(pk=getattr(invoice, "pk", None)).first()
    if svezi is None:
        pripravi_pdf_context(invoice)
    if svezi.document_type == Invoice.DocumentType.ST and transaction.get_connection().in_atomic_block:
        raise PdfRenderNapaka("PDF_TRANSAKCIJA", "ST PDF mora nastati zunaj transakcije.")
    obstojeci = _preveri_obstojeci_artifact(svezi)
    if obstojeci is not None:
        return obstojeci

    context = pripravi_pdf_context(svezi)
    ime_datoteke = f"{context.stevilka}.pdf"
    try:
        relativna_pot, pot = varna_render_pot_podjetja(
            svezi.company_id, ime_datoteke
        )
    except NeveljavnaRenderPot as napaka:
        raise PdfRenderNapaka("PDF_POT_NEVELJAVNA", "PDF ime ni veljavno.") from napaka
    pdf_bytes = _generiraj_pdf_bytes(context)
    with _zakleni_artifact(pot):
        svezi = Invoice.objects.get(pk=invoice.pk)
        obstojeci = _preveri_obstojeci_artifact(svezi)
        if obstojeci is not None:
            if obstojeci.pdf_bytes != pdf_bytes:
                raise PdfRenderNapaka(
                "PDF_SOCASNI_RENDER_NESKLADEN",
                    "Sočasni PDF render je ustvaril drugačne bytes.",
                )
            return obstojeci
        ustvarjen = _objavi_atomsko(
            pot, pdf_bytes, tip="PDF", napaka_razred=PdfRenderNapaka
        )
        koncni_bytes = pot.read_bytes()
        checksum = hashlib.sha256(koncni_bytes).hexdigest()
        try:
            nastavi_pdf_artifact(
                invoice, rendered_pdf_path=relativna_pot, checksum=checksum
            )
        except Exception as napaka:
            stanje = _stanje_pdf_metapodatkov(invoice, relativna_pot, checksum)
            if stanje == "ISTI":
                return PdfRenderRezultat(relativna_pot, checksum, koncni_bytes)
            if stanje == "PRAZNI" and ustvarjen:
                _odstrani_ustvarjen_artifact(
                    pot,
                    koncni_bytes,
                    tip="PDF",
                    napaka_razred=PdfRenderNapaka,
                )
            raise PdfRenderNapaka(
                "PDF_METAPODATKI_NISO_SHRANJENI",
                "PDF metapodatki niso shranjeni.",
            ) from napaka
        return PdfRenderRezultat(relativna_pot, checksum, koncni_bytes)


def stanje_koncnega_pdf(invoice: Invoice) -> dict:
    svezi = Invoice.objects.get(pk=invoice.pk)
    if not svezi.rendered_pdf_path and not svezi.checksum:
        return {"state": "PENDING", "error": None}
    try:
        rezultat = _preveri_obstojeci_artifact(svezi)
    except PdfRenderNapaka as napaka:
        return {"state": "ERROR", "error": str(napaka)}
    return {"state": "READY", "error": None, "result": rezultat}


def renderiraj_koncne_pdfje_mesecnega_paketa(cycle):
    from invoicing.models import MonthlyCycle
    from invoicing.services import (
        MesecniCikelNapaka,
        aktivni_izracuni_mesecnega_cikla,
        stanje_zamrznjenega_mesecnega_kandidata,
        trenutni_pregledi_mesecnega_cikla,
    )

    cycle = MonthlyCycle.objects.get(pk=cycle.pk)
    if cycle.trenutni_korak != MonthlyCycle.Korak.POTRDITEV_PAKETA:
        raise MesecniCikelNapaka("Končne PDF-je je mogoče pripraviti samo v koraku 5.")
    reviews = {
        (review.batch_id, review.lease_id, review.candidate_fingerprint): review
        for review in trenutni_pregledi_mesecnega_cikla(cycle)
    }
    results = []
    for batch in aktivni_izracuni_mesecnega_cikla(cycle).order_by("company_id", "pk"):
        for candidate in batch.calculation_snapshot.get("candidates", []):
            identity = (
                batch.pk,
                candidate.get("lease_id"),
                candidate.get("candidate_fingerprint"),
            )
            review = reviews.get(identity)
            stanje = stanje_zamrznjenega_mesecnega_kandidata(*identity)
            result = {
                "batch_id": identity[0],
                "lease_id": identity[1],
                "candidate_fingerprint": identity[2],
                "ok": False,
            }
            if review is None or stanje["state"] != "MATERIALIZED":
                result["error"] = stanje["error"] or "Zamrznjeni kandidat še ni materializiran."
            elif not review.selected:
                result.update(ok=True, skipped=True, invoice=stanje["invoice"])
            else:
                invoice = stanje["invoice"]
                try:
                    artifact = render_pdf(invoice)
                    svezi = Invoice.objects.get(pk=invoice.pk)
                    pdf_state = stanje_koncnega_pdf(svezi)
                    if pdf_state["state"] != "READY":
                        raise PdfRenderNapaka("PDF_INTEGRITETA", pdf_state["error"] or "PDF ni pripravljen.")
                except PdfRenderNapaka as napaka:
                    result["error"] = str(napaka)
                else:
                    result.update(ok=True, skipped=False, invoice=svezi, artifact=artifact)
            results.append(result)
    return results


def render_eslog(invoice: Invoice) -> EslogRenderRezultat:
    svezi = Invoice.objects.get(pk=invoice.pk)
    obstojeci = _preveri_obstojeci_eslog_artifact(svezi)
    if obstojeci is not None:
        return obstojeci

    xml_bytes = generiraj_eslog(svezi)
    ime_datoteke = f"{svezi.stevilka}.xml"
    try:
        relativna_pot, pot = varna_render_pot_podjetja(
            svezi.company_id, ime_datoteke
        )
    except NeveljavnaRenderPot as napaka:
        raise EslogRenderNapaka(
            "ESLOG_POT_NEVELJAVNA", "eSLOG ime ni veljavno."
        ) from napaka
    with _zakleni_artifact(pot):
        svezi = Invoice.objects.get(pk=invoice.pk)
        obstojeci = _preveri_obstojeci_eslog_artifact(svezi)
        if obstojeci is not None:
            if obstojeci.xml_bytes != xml_bytes:
                raise EslogRenderNapaka(
                "ESLOG_SOCASNI_RENDER_NESKLADEN",
                    "Sočasni eSLOG render je ustvaril drugačne bytes.",
                )
            return obstojeci
        ustvarjen = _objavi_atomsko(
            pot, xml_bytes, tip="ESLOG", napaka_razred=EslogRenderNapaka
        )
        koncni_bytes = pot.read_bytes()
        checksum = hashlib.sha256(koncni_bytes).hexdigest()
        try:
            nastavi_eslog_artifact(
                invoice, rendered_eslog_path=relativna_pot, checksum=checksum
            )
        except Exception as napaka:
            stanje = _stanje_eslog_metapodatkov(invoice, relativna_pot, checksum)
            if stanje == "ISTI":
                return EslogRenderRezultat(relativna_pot, checksum, koncni_bytes)
            if stanje == "PRAZNI" and ustvarjen:
                _odstrani_ustvarjen_artifact(
                    pot,
                    koncni_bytes,
                    tip="ESLOG",
                    napaka_razred=EslogRenderNapaka,
                )
            raise EslogRenderNapaka(
                "ESLOG_METAPODATKI_NISO_SHRANJENI",
                "eSLOG metapodatki niso shranjeni.",
            ) from napaka
        return EslogRenderRezultat(relativna_pot, checksum, koncni_bytes)

def render_sample_pdf(lease, *, datum_izdaje, obdobje_od, obdobje_do) -> PdfRenderRezultat:
    from companies.models import Company, SamplePdfPreview
    from companies.services import (
        FINGERPRINT_VZORCA_VERSION,
        _kanonicni_fingerprint,
        izracunaj_fingerprint_vzorca,
        pripravi_fingerprint_predogleda,
    )

    company = Company.objects.get(pk=lease.unit.property.company_id)
    fingerprint = pripravi_fingerprint_predogleda(company, lease, datum_izdaje=datum_izdaje)
    context = pripravi_vzorcni_pdf_context(
        lease,
        datum_izdaje=datum_izdaje,
        obdobje_od=obdobje_od,
        obdobje_do=obdobje_do,
    )
    ime_datoteke = f"VZOREC-{slugify_naziv(context.izdajatelj_naziv)}.pdf"
    try:
        company_id = Lease.objects.values_list(
            "unit__property__company_id", flat=True
        ).get(pk=lease.pk)
        relativna_pot, pot = varna_render_pot_podjetja(company_id, ime_datoteke)
    except NeveljavnaRenderPot as napaka:
        raise PdfRenderNapaka("PDF_POT_NEVELJAVNA", "PDF ime ni veljavno.") from napaka
    pdf_bytes = _generiraj_pdf_bytes(context)
    with _zakleni_artifact(pot):
        _zamenjaj_atomsko(pot, pdf_bytes)
    with transaction.atomic():
        if (
            fingerprint != izracunaj_fingerprint_vzorca(company, lease, datum_izdaje=datum_izdaje)
            or context.pdf_template_version != settings.PDF_TEMPLATE_VERSION
        ):
            raise ValueError("Podatki so se med PDF predogledom spremenili; poskusite znova.")
        preview = SamplePdfPreview.objects.create(
            company=company, lease=lease, source_fingerprint=fingerprint,
            fingerprint_schema_version=FINGERPRINT_VZORCA_VERSION,
            pdf_template_version=context.pdf_template_version,
            pdf_checksum=hashlib.sha256(pdf_bytes).hexdigest(),
            content_fingerprint=_kanonicni_fingerprint(asdict(context)),
            datum_izdaje=datum_izdaje, obdobje_od=obdobje_od, obdobje_do=obdobje_do,
        )
    return PdfRenderRezultat(relativna_pot, preview.pdf_checksum, pdf_bytes, preview.pk)
