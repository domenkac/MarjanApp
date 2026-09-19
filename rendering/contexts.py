from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import SuspiciousFileOperation
from django.core.files.storage import default_storage

from catalogs.models import VatRegime
from companies.services import izberi_veljavnega_podpisnika
from contracts.models import Lease
from invoicing.models import Invoice, InvoiceLine
from invoicing.services import izracunaj_kandidata


class PdfRenderNapaka(ValueError):
    def __init__(self, koda: str, sporocilo: str):
        self.koda = koda
        self.artifact_tip = "PDF"
        super().__init__(sporocilo)


@dataclass(frozen=True, slots=True)
class PdfVrstica:
    opis: str
    osnova: Decimal
    ddv_stopnja: Decimal
    ddv_znesek: Decimal
    skupaj: Decimal
    ddv_nacin_obracuna: str
    ddv_klavzula: str
    ddv_rekap_skupina: str
    je_sorazmerno: bool


@dataclass(frozen=True, slots=True)
class PdfContext:
    document_type: str
    stevilka: str | None
    datum_izdaje: date
    obdobje_od: date
    obdobje_do: date
    rok_placila: date | None
    prejemnik_naziv: str
    prejemnik_naslov: str
    prejemnik_davcna_stevilka: str
    prejemnik_maticna_stevilka: str
    izdajatelj_naziv: str
    izdajatelj_naslov: str
    izdajatelj_davcna_stevilka: str
    izdajatelj_maticna_stevilka: str
    izdajatelj_trr: str
    izdajatelj_logotip_slika_pot: str
    izdajatelj_podpisnik_ime: str
    izdajatelj_podpisnik_funkcija: str
    izdajatelj_podpisnik_slika_pot: str
    oznaka_pogodbe: str | None
    objekt_naziv: str | None
    povezan_racun_stevilka: str | None
    razlog: str
    osnova_skupaj: Decimal
    ddv_skupaj: Decimal
    skupaj_z_ddv: Decimal
    vrstice: tuple[PdfVrstica, ...]
    pdf_template_version: str
    oznaka_vzorca: str | None = None


_OBVEZNI_SNAPSHOTI = (
    "prejemnik_naziv",
    "prejemnik_naslov",
    "izdajatelj_naziv",
    "izdajatelj_naslov",
    "izdajatelj_davcna_stevilka",
    "izdajatelj_maticna_stevilka",
    "izdajatelj_trr",
    "izdajatelj_logotip_slika_pot",
    "izdajatelj_podpisnik_ime",
    "izdajatelj_podpisnik_slika_pot",
    "pdf_template_version",
)


def _preveri_polja(zapis, polja):
    for polje in polja:
        if not str(getattr(zapis, polje, "") or "").strip():
            raise PdfRenderNapaka("SNAPSHOT_MANJKA", f"Manjka obvezen podatek: {polje}.")


def _preveri_sliko(pot: str):
    try:
        obstaja = default_storage.exists(pot)
    except (OSError, SuspiciousFileOperation, ValueError) as napaka:
        raise PdfRenderNapaka("ASSET_NEVELJAVEN", f"Slika ni dovoljena: {pot}.") from napaka
    if not obstaja:
        raise PdfRenderNapaka("ASSET_MANJKA", f"Slika ne obstaja: {pot}.")


def _vrstica_context(vrstica: InvoiceLine) -> PdfVrstica:
    return PdfVrstica(
        opis=vrstica.opis,
        osnova=vrstica.osnova,
        ddv_stopnja=vrstica.ddv_stopnja,
        ddv_znesek=vrstica.ddv_znesek,
        skupaj=vrstica.skupaj,
        ddv_nacin_obracuna=vrstica.ddv_nacin_obracuna,
        ddv_klavzula=vrstica.ddv_klavzula,
        ddv_rekap_skupina=vrstica.ddv_rekap_skupina,
        je_sorazmerno=vrstica.je_sorazmerno,
    )


def preveri_pdf_predpogoje(invoice: Invoice) -> tuple[Invoice, tuple[InvoiceLine, ...]]:
    if not isinstance(invoice, Invoice) or invoice.pk is None:
        raise PdfRenderNapaka("RACUN_NE_OBSTAJA", "Račun ne obstaja.")
    svezi = Invoice.objects.filter(pk=invoice.pk).first()
    if svezi is None:
        raise PdfRenderNapaka("RACUN_NE_OBSTAJA", "Račun ne obstaja.")
    if svezi.status == Invoice.Status.ARHIVIRAN_NI_IZDAN:
        raise PdfRenderNapaka("NEIZDAN_ZAPIS", "Migracijski zapis ni izdan račun.")
    if not svezi.stevilka:
        raise PdfRenderNapaka("STEVILKA_MANJKA", "Račun nima številke.")
    if svezi.document_type not in Invoice.DocumentType.values:
        raise PdfRenderNapaka("TIP_NEVELJAVEN", "Tip dokumenta ni veljaven.")
    _preveri_polja(svezi, _OBVEZNI_SNAPSHOTI)
    if svezi.document_type in (Invoice.DocumentType.NP, Invoice.DocumentType.ST) and svezi.rok_placila is None:
        raise PdfRenderNapaka("SNAPSHOT_MANJKA", "Manjka rok plačila.")
    if svezi.document_type == Invoice.DocumentType.NP and not svezi.oznaka_pogodbe:
        raise PdfRenderNapaka("SNAPSHOT_MANJKA", "Manjka oznaka pogodbe.")
    if svezi.document_type in (
        Invoice.DocumentType.STORNO, Invoice.DocumentType.DOBROPIS
    ):
        if not (svezi.razlog or "").strip() or not svezi.povezan_racun_id:
            raise PdfRenderNapaka(
                "KOREKCIJA_NEPOPOLNA", "Korekcija nima razloga ali izvirnega računa."
            )
        if not svezi.povezan_racun.stevilka:
            raise PdfRenderNapaka(
                "KOREKCIJA_NEPOPOLNA", "Izvirni račun nima številke."
            )
    _preveri_sliko(svezi.izdajatelj_logotip_slika_pot)
    _preveri_sliko(svezi.izdajatelj_podpisnik_slika_pot)

    vrstice = tuple(InvoiceLine.objects.filter(invoice=svezi).order_by("pk"))
    if not vrstice:
        raise PdfRenderNapaka("VRSTICE_MANJKAJO", "Račun nima vrstic.")
    for vrstica in vrstice:
        _preveri_polja(vrstica, ("opis", "ddv_nacin_obracuna", "ddv_rekap_skupina"))
        if (
            vrstica.ddv_nacin_obracuna
            in (VatRegime.NacinObracuna.OPROSCENO, VatRegime.NacinObracuna.OBRNJENA_DAVCNA)
            and not vrstica.ddv_klavzula.strip()
        ):
            raise PdfRenderNapaka("KLAVZULA_MANJKA", "Vrstica nima obvezne DDV klavzule.")
        if vrstica.skupaj != vrstica.osnova + vrstica.ddv_znesek:
            raise PdfRenderNapaka("VSOTE_NESKLADNE", "Vsota vrstice ni konsistentna.")
    if any(
        getattr(svezi, polje) is None
        for polje in ("osnova_skupaj", "ddv_skupaj", "skupaj_z_ddv")
    ):
        raise PdfRenderNapaka("VSOTE_MANJKAJO", "Račun nima shranjenih vsot.")
    if (
        sum((v.osnova for v in vrstice), Decimal("0.00")) != svezi.osnova_skupaj
        or sum((v.ddv_znesek for v in vrstice), Decimal("0.00")) != svezi.ddv_skupaj
        or sum((v.skupaj for v in vrstice), Decimal("0.00")) != svezi.skupaj_z_ddv
        or svezi.osnova_skupaj + svezi.ddv_skupaj != svezi.skupaj_z_ddv
    ):
        raise PdfRenderNapaka("VSOTE_NESKLADNE", "Vsote računa se ne ujemajo z vrsticami.")
    return svezi, vrstice


def pripravi_pdf_context(invoice: Invoice) -> PdfContext:
    svezi, vrstice = preveri_pdf_predpogoje(invoice)
    return PdfContext(
        document_type=svezi.document_type,
        stevilka=svezi.stevilka,
        datum_izdaje=svezi.datum_izdaje,
        obdobje_od=svezi.obdobje_od,
        obdobje_do=svezi.obdobje_do,
        rok_placila=svezi.rok_placila,
        prejemnik_naziv=svezi.prejemnik_naziv,
        prejemnik_naslov=svezi.prejemnik_naslov,
        prejemnik_davcna_stevilka=svezi.prejemnik_davcna_stevilka,
        prejemnik_maticna_stevilka=svezi.prejemnik_maticna_stevilka,
        izdajatelj_naziv=svezi.izdajatelj_naziv,
        izdajatelj_naslov=svezi.izdajatelj_naslov,
        izdajatelj_davcna_stevilka=svezi.izdajatelj_davcna_stevilka,
        izdajatelj_maticna_stevilka=svezi.izdajatelj_maticna_stevilka,
        izdajatelj_trr=svezi.izdajatelj_trr,
        izdajatelj_logotip_slika_pot=svezi.izdajatelj_logotip_slika_pot,
        izdajatelj_podpisnik_ime=svezi.izdajatelj_podpisnik_ime,
        izdajatelj_podpisnik_funkcija=svezi.izdajatelj_podpisnik_funkcija,
        izdajatelj_podpisnik_slika_pot=svezi.izdajatelj_podpisnik_slika_pot,
        oznaka_pogodbe=svezi.oznaka_pogodbe,
        objekt_naziv=svezi.objekt_naziv,
        povezan_racun_stevilka=(
            svezi.povezan_racun.stevilka if svezi.povezan_racun_id else None
        ),
        razlog=svezi.razlog,
        osnova_skupaj=svezi.osnova_skupaj,
        ddv_skupaj=svezi.ddv_skupaj,
        skupaj_z_ddv=svezi.skupaj_z_ddv,
        vrstice=tuple(_vrstica_context(v) for v in vrstice),
        pdf_template_version=svezi.pdf_template_version,
    )


def pripravi_vzorcni_pdf_context(
    lease: Lease, *, datum_izdaje: date, obdobje_od: date, obdobje_do: date
) -> PdfContext:
    if not isinstance(lease, Lease) or lease.pk is None:
        raise PdfRenderNapaka("POGODBA_NE_OBSTAJA", "Pogodba ne obstaja.")
    lease = Lease.objects.select_related("tenant", "unit__property__company").filter(pk=lease.pk).first()
    if lease is None:
        raise PdfRenderNapaka("POGODBA_NE_OBSTAJA", "Pogodba ne obstaja.")
    kandidat = izracunaj_kandidata(lease, obdobje_od, obdobje_do, datum_izdaje)
    if not kandidat.se_izda:
        raise PdfRenderNapaka("VZOREC_NEVELJAVEN", kandidat.razlog_neizdaje or "Ni obračunljivih vrstic.")
    company = lease.unit.property.company
    tenant = lease.tenant
    try:
        podpisnik = izberi_veljavnega_podpisnika(company, datum_izdaje)
    except ValueError as napaka:
        raise PdfRenderNapaka("PODPISNIK_NEVELJAVEN", str(napaka)) from napaka
    for zapis, polja in (
        (company, ("naziv", "naslov", "davcna_stevilka", "maticna_stevilka", "trr")),
        (tenant, ("naziv", "naslov")),
        (podpisnik, ("ime_priimek",)),
    ):
        _preveri_polja(zapis, polja)
    _preveri_sliko(company.logotip.name)
    _preveri_sliko(podpisnik.podpis_slika.name)
    vrstice = tuple(_vrstica_context(v) for v in kandidat.vrstice)
    osnova = sum((v.osnova for v in vrstice), Decimal("0.00"))
    ddv = sum((v.ddv_znesek for v in vrstice), Decimal("0.00"))
    return PdfContext(
        document_type=Invoice.DocumentType.NP,
        stevilka=None,
        datum_izdaje=datum_izdaje,
        obdobje_od=obdobje_od,
        obdobje_do=obdobje_do,
        rok_placila=datum_izdaje + timedelta(days=lease.rok_placila_dni),
        prejemnik_naziv=tenant.naziv,
        prejemnik_naslov=tenant.naslov,
        prejemnik_davcna_stevilka=tenant.davcna_stevilka,
        prejemnik_maticna_stevilka=tenant.maticna_stevilka,
        izdajatelj_naziv=company.naziv,
        izdajatelj_naslov=company.naslov,
        izdajatelj_davcna_stevilka=company.davcna_stevilka,
        izdajatelj_maticna_stevilka=company.maticna_stevilka,
        izdajatelj_trr=company.trr,
        izdajatelj_logotip_slika_pot=company.logotip.name,
        izdajatelj_podpisnik_ime=podpisnik.ime_priimek,
        izdajatelj_podpisnik_funkcija=podpisnik.funkcija,
        izdajatelj_podpisnik_slika_pot=podpisnik.podpis_slika.name,
        oznaka_pogodbe=lease.oznaka_pogodbe,
        objekt_naziv=lease.unit.property.naziv,
        povezan_racun_stevilka=None,
        razlog="",
        osnova_skupaj=osnova,
        ddv_skupaj=ddv,
        skupaj_z_ddv=osnova + ddv,
        vrstice=vrstice,
        pdf_template_version=settings.PDF_TEMPLATE_VERSION,
        oznaka_vzorca="VZOREC \u2013 NI RA\u010cUN",
    )