import hashlib
import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path, PurePosixPath

from django.conf import settings
from django.core.exceptions import SuspiciousFileOperation, ValidationError
from django.core.files.storage import default_storage
from PIL import Image

from catalogs.models import VatRegime
from companies.models import Company
from companies.services import izberi_veljavnega_podpisnika
from contracts.models import Lease, Tenant
from contracts.vat_eligibility import preveri_upravicenost_prejemnika
from core.pravila import izracunaj_iz_bruto
from core.validators import NAJVECJA_SLIKA, preveri_sliko
from invoicing.models import Invoice, InvoiceLine
from invoicing.services import (
    _company_validation_issues,
    _tenant_validation_issues,
    izracunaj_kandidata,
)
from invoicing.st_confirmation import preveri_st_pdf_referencne_zahteve


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
    enota_mere: str = ""
    referenca_listine: str = ""
    datum_reference: date | None = None


class EslogRenderNapaka(ValueError):
    def __init__(self, koda: str, sporocilo: str):
        self.koda = koda
        self.artifact_tip = "ESLOG"
        super().__init__(sporocilo)


@dataclass(frozen=True, slots=True)
class EslogVrstica:
    opis: str
    osnova: Decimal
    ddv_stopnja: Decimal
    ddv_znesek: Decimal
    skupaj: Decimal
    ddv_eslog_koda: str
    ddv_klavzula: str


@dataclass(frozen=True, slots=True)
class EslogKonfiguracijaContext:
    elektronski_naslov_kupca_vkljucen: bool
    kontaktni_podatki_kupca_vkljuceni: bool
    elektronski_naslov_kupca: str | None
    kontaktna_tocka_kupca: str | None
    kontaktni_telefon_kupca: str | None
    kontaktni_email_kupca: str | None


@dataclass(frozen=True, slots=True)
class EslogContext:
    document_type: str
    stevilka: str
    datum_izdaje: date
    rok_placila: date | None
    prejemnik_naziv: str
    prejemnik_naslov: str
    prejemnik_drzava_koda: str
    prejemnik_identificiran_za_ddv: bool
    prejemnik_davcna_stevilka: str
    prejemnik_ddv_identifikacijska_stevilka: str
    prejemnik_maticna_stevilka: str
    prejemnik_tip: str
    izdajatelj_naziv: str
    izdajatelj_naslov: str
    izdajatelj_drzava_koda: str
    izdajatelj_identificiran_za_ddv: bool
    izdajatelj_davcna_stevilka: str
    izdajatelj_maticna_stevilka: str
    izdajatelj_trr: str
    oznaka_pogodbe: str | None
    povezan_racun_stevilka: str | None
    povezan_racun_datum: date | None
    osnova_skupaj: Decimal
    ddv_skupaj: Decimal
    skupaj_z_ddv: Decimal
    vrstice: tuple[EslogVrstica, ...]
    eslog_konfiguracija: EslogKonfiguracijaContext


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
            raise PdfRenderNapaka(
                "SNAPSHOT_MANJKA", f"Manjka obvezen podatek: {polje}."
            )


def _preveri_sliko(pot: str):
    try:
        obstaja = default_storage.exists(pot)
    except (OSError, SuspiciousFileOperation, ValueError) as napaka:
        raise PdfRenderNapaka(
            "ASSET_NEVELJAVEN", f"Slika ni dovoljena: {pot}."
        ) from napaka
    if not obstaja:
        raise PdfRenderNapaka("ASSET_MANJKA", f"Slika ne obstaja: {pot}.")


def _snapshot_asset(checksum: str) -> str:
    if len(checksum or "") != 64 or any(c not in "0123456789abcdef" for c in checksum):
        raise PdfRenderNapaka("ASSET_NEVELJAVEN", "Nespremenljiv asset ni veljaven.")
    pot = f"nespremenljivi-asseti/{checksum}"
    preveri_nespremenljiv_asset(pot)
    return pot


def preveri_nespremenljiv_asset(pot: str) -> str:
    preberi_pdf_sliko(pot)
    return pot


def preberi_pdf_sliko(pot: str, *, nespremenljiva: bool = True) -> tuple[bytes, str]:
    """Return the exact verified content for embedding, never a reopen instruction.

    Only the unnumbered setup sample may use mutable upload paths. Issued NP/ST,
    their corrections and frozen monthly previews require a content address.
    """
    if (not isinstance(pot, str) or not pot or "\\" in pot or ":" in pot
            or pot.startswith("/") or ".." in PurePosixPath(pot).parts
            or PurePosixPath(pot).as_posix() != pot):
        raise PdfRenderNapaka("ASSET_NEVELJAVEN", "Pot slike ni veljavna.")
    digest = None
    if nespremenljiva or pot.startswith("nespremenljivi-asseti/"):
        match = re.fullmatch(r"nespremenljivi-asseti/([0-9a-f]{64})", pot)
        if match is None:
            raise PdfRenderNapaka("ASSET_NEVELJAVEN", "Pot nespremenljivega asseta ni veljavna.")
        digest = match[1]
    try:
        root = Path(settings.MEDIA_ROOT).resolve(strict=True)
        path = Path(default_storage.path(pot))
        # Reject aliases, including symlinks that point back inside MEDIA_ROOT.
        if path.resolve() != root.joinpath(*PurePosixPath(pot).parts):
            raise PdfRenderNapaka("ASSET_NEVELJAVEN", "Pot slike ni dovoljena.")
        with default_storage.open(pot, "rb") as datoteka:
            vsebina = datoteka.read(NAJVECJA_SLIKA + 1)
    except (SuspiciousFileOperation, ValueError, RuntimeError) as napaka:
        raise PdfRenderNapaka("ASSET_NEVELJAVEN", "Pot slike ni dovoljena.") from napaka
    except OSError as napaka:
        raise PdfRenderNapaka("ASSET_MANJKA", "Slika ni dostopna.") from napaka
    if digest is not None and hashlib.sha256(vsebina).hexdigest() != digest:
        raise PdfRenderNapaka("ASSET_NEVELJAVEN", "Nespremenljiv asset je poškodovan.")
    try:
        preveri_sliko(BytesIO(vsebina))
        with Image.open(BytesIO(vsebina)) as slika:
            mime = Image.MIME[slika.format]
    except ValidationError as napaka:
        raise PdfRenderNapaka("ASSET_NEVELJAVEN", "Slika mora biti varna datoteka JPG ali PNG.") from napaka
    return vsebina, mime


def _snapshot_vrednost(zapis, kljuc):
    vrednost = zapis.get(kljuc) if isinstance(zapis, dict) else None
    if vrednost is None or (isinstance(vrednost, str) and not vrednost.strip()):
        raise PdfRenderNapaka("SNAPSHOT_MANJKA", "Manjka obvezen podatek v posnetku.")
    return vrednost


def pripravi_mesecni_pdf_context(snapshot: dict, candidate: dict) -> PdfContext:
    try:
        company = _snapshot_vrednost(snapshot, "company")
        tenant = _snapshot_vrednost(candidate, "tenant")
        signatory = _snapshot_vrednost(company, "signatory")
        lines = _snapshot_vrednost(candidate, "lines")
        totals = _snapshot_vrednost(candidate, "totals")
        if not candidate.get("will_issue") or not lines:
            raise PdfRenderNapaka(
                "SNAPSHOT_MANJKA", "Kandidat nima vsebine za predogled PDF."
            )
        vrstice = tuple(
            PdfVrstica(
                opis=str(_snapshot_vrednost(line, "description")),
                osnova=Decimal(str(_snapshot_vrednost(line, "net"))),
                ddv_stopnja=Decimal(str(_snapshot_vrednost(line, "vat_rate"))),
                ddv_znesek=Decimal(str(_snapshot_vrednost(line, "vat"))),
                skupaj=Decimal(str(_snapshot_vrednost(line, "total"))),
                ddv_nacin_obracuna=str(_snapshot_vrednost(line, "vat_method")),
                ddv_klavzula=str(line.get("vat_clause") or ""),
                ddv_rekap_skupina=str(_snapshot_vrednost(line, "vat_summary_group")),
                je_sorazmerno=bool(line.get("proportional")),
            )
            for line in lines
        )
        osnova = Decimal(str(_snapshot_vrednost(totals, "net")))
        ddv = Decimal(str(_snapshot_vrednost(totals, "vat")))
        skupaj = Decimal(str(_snapshot_vrednost(totals, "total")))
        if (
            sum((line.osnova for line in vrstice), Decimal("0.00")) != osnova
            or sum((line.ddv_znesek for line in vrstice), Decimal("0.00")) != ddv
            or sum((line.skupaj for line in vrstice), Decimal("0.00")) != skupaj
            or osnova + ddv != skupaj
        ):
            raise PdfRenderNapaka("VSOTE_NESKLADNE", "Vsote v posnetku niso skladne.")
        return PdfContext(
            document_type=Invoice.DocumentType.NP,
            stevilka="PREDOGLED",
            datum_izdaje=date.fromisoformat(str(_snapshot_vrednost(snapshot, "issue_date"))),
            obdobje_od=date.fromisoformat(str(_snapshot_vrednost(snapshot, "period_start"))),
            obdobje_do=date.fromisoformat(str(_snapshot_vrednost(snapshot, "period_end"))),
            rok_placila=date.fromisoformat(str(_snapshot_vrednost(candidate, "due_date"))),
            prejemnik_naziv=str(_snapshot_vrednost(tenant, "name")),
            prejemnik_naslov=str(_snapshot_vrednost(tenant, "address")),
            prejemnik_davcna_stevilka=str(tenant.get("tax_number") or ""),
            prejemnik_maticna_stevilka=str(tenant.get("registration_number") or ""),
            izdajatelj_naziv=str(_snapshot_vrednost(company, "name")),
            izdajatelj_naslov=str(_snapshot_vrednost(company, "address")),
            izdajatelj_davcna_stevilka=str(_snapshot_vrednost(company, "tax_number")),
            izdajatelj_maticna_stevilka=str(_snapshot_vrednost(company, "registration_number")),
            izdajatelj_trr=str(_snapshot_vrednost(company, "iban")),
            izdajatelj_logotip_slika_pot=_snapshot_asset(
                str(_snapshot_vrednost(company, "logo_checksum"))
            ),
            izdajatelj_podpisnik_ime=str(_snapshot_vrednost(signatory, "name")),
            izdajatelj_podpisnik_funkcija=str(signatory.get("function") or ""),
            izdajatelj_podpisnik_slika_pot=_snapshot_asset(
                str(_snapshot_vrednost(signatory, "signature_checksum"))
            ),
            oznaka_pogodbe=str(_snapshot_vrednost(candidate, "contract_reference")),
            objekt_naziv=str(_snapshot_vrednost(candidate, "property_label")),
            povezan_racun_stevilka=None,
            razlog="",
            osnova_skupaj=osnova,
            ddv_skupaj=ddv,
            skupaj_z_ddv=skupaj,
            vrstice=vrstice,
            pdf_template_version=str(_snapshot_vrednost(candidate, "pdf_template_version")),
            oznaka_vzorca="PREDOGLED – NI RAČUN",
        )
    except PdfRenderNapaka:
        raise
    except (ArithmeticError, TypeError, ValueError) as napaka:
        raise PdfRenderNapaka(
            "SNAPSHOT_NEVELJAVEN", "Podatki posnetka za PDF niso veljavni."
        ) from napaka


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
        enota_mere=vrstica.enota_mere,
        referenca_listine=vrstica.referenca_listine,
        datum_reference=vrstica.datum_reference,
    )


def _preveri_st_posnetek(invoice: Invoice, vrstice: tuple[InvoiceLine, ...]) -> None:
    """Validate confirmed ST values only; never resolve current catalogs or leases."""
    izdani_statusi = {
        Invoice.Status.IZBRAN_ZA_POSILJANJE, Invoice.Status.POSLAN,
        Invoice.Status.NAPAKA, Invoice.Status.REZULTAT_NI_ZNAN,
        Invoice.Status.STORNIRAN, Invoice.Status.POPRAVLJEN,
    }
    stevilka = re.fullmatch(r"ST-([0-9]{4})-([0-9]{4,})", invoice.stevilka)
    if (invoice.status not in izdani_statusi or stevilka is None
            or int(stevilka[1]) != invoice.datum_izdaje.year
            or int(stevilka[2]) < 1
            or stevilka[2] != f"{int(stevilka[2]):04d}"):
        raise PdfRenderNapaka("ST_NEPOTRJEN", "ST nima veljavne potrditve ali številke.")
    if (invoice.obdobje_od > invoice.obdobje_do
            or invoice.rok_placila < invoice.datum_izdaje):
        raise PdfRenderNapaka("SNAPSHOT_NEVELJAVEN", "Datumi ST niso veljavni.")
    _preveri_polja(invoice, ("prejemnik_tip", "prejemnik_drzava_koda", "izdajatelj_drzava_koda"))
    if (invoice.prejemnik_tip not in {"PRAVNA_OSEBA", "FIZICNA_OSEBA"}
            or type(invoice.prejemnik_identificiran_za_ddv) is not bool
            or type(invoice.prejemnik_polni_odbitek) is not bool
            or type(invoice.izdajatelj_identificiran_za_ddv) is not bool):
        raise PdfRenderNapaka("SNAPSHOT_NEVELJAVEN", "Davčni posnetek ST ni veljaven.")
    izdajatelj = Company(**{
        polje: getattr(invoice, f"izdajatelj_{polje}")
        for polje in ("naziv", "naslov", "davcna_stevilka", "maticna_stevilka", "trr", "drzava_koda")
    })
    prejemnik = Tenant(
        **{polje: getattr(invoice, f"prejemnik_{polje}") for polje in (
            "tip", "naziv", "naslov", "drzava_koda", "davcna_stevilka",
            "maticna_stevilka", "ddv_identifikacijska_stevilka", "identificiran_za_ddv",
        )},
        ddv_zavezanec_s_polnim_odbitkom=invoice.prejemnik_polni_odbitek,
    )
    if _company_validation_issues(izdajatelj) or _tenant_validation_issues(prejemnik):
        raise PdfRenderNapaka("SNAPSHOT_NEVELJAVEN", "Posnetek izdajatelja ali prejemnika ST ni veljaven.")
    if invoice.pdf_template_version == "2-st":
        try:
            preveri_st_pdf_referencne_zahteve(invoice, vrstice)
        except ValidationError as napaka:
            raise PdfRenderNapaka("ST_REFERENCA_NEVELJAVNA", str(napaka)) from napaka
    preveri_nespremenljiv_asset(invoice.izdajatelj_logotip_slika_pot)
    preveri_nespremenljiv_asset(invoice.izdajatelj_podpisnik_slika_pot)
    for vrstica in vrstice:
        _preveri_polja(vrstica, ("enota_mere", "ddv_rezim_sifra"))
        if (vrstica.ddv_nacin_obracuna not in VatRegime.NacinObracuna.values
                or type(vrstica.ddv_zahteva_polni_odbitek) is not bool
                or bool(vrstica.referenca_listine.strip()) != (vrstica.datum_reference is not None)):
            raise PdfRenderNapaka("SNAPSHOT_NEVELJAVEN", "Posnetek ST vrstice ni veljaven.")
        try:
            preveri_upravicenost_prejemnika(
                sifra=vrstica.ddv_rezim_sifra, zahteva_polni_odbitek=vrstica.ddv_zahteva_polni_odbitek,
                tip=invoice.prejemnik_tip, identificiran_za_ddv=invoice.prejemnik_identificiran_za_ddv,
                polni_odbitek=invoice.prejemnik_polni_odbitek,
                najemnik=invoice.prejemnik_naziv, postavka=vrstica.opis,
            )
        except ValidationError as napaka:
            raise PdfRenderNapaka("SNAPSHOT_NEVELJAVEN", str(napaka)) from napaka
        stopnja = (vrstica.ddv_stopnja
                   if vrstica.ddv_nacin_obracuna == VatRegime.NacinObracuna.OBDAVCENO
                   else Decimal("0.00"))
        # Verification uses the persisted gross/rate; output retains stored amounts.
        if (vrstica.skupaj <= 0 or vrstica.ddv_stopnja < 0
                or (vrstica.osnova, vrstica.ddv_znesek) != izracunaj_iz_bruto(vrstica.skupaj, stopnja)):
            raise PdfRenderNapaka("VSOTE_NESKLADNE", "Denarni posnetek ST ni skladen.")


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
    if (
        svezi.document_type in (Invoice.DocumentType.NP, Invoice.DocumentType.ST)
        and svezi.rok_placila is None
    ):
        raise PdfRenderNapaka("SNAPSHOT_MANJKA", "Manjka rok plačila.")
    if svezi.document_type == Invoice.DocumentType.NP and not svezi.oznaka_pogodbe:
        raise PdfRenderNapaka("SNAPSHOT_MANJKA", "Manjka oznaka pogodbe.")
    if svezi.document_type in (
        Invoice.DocumentType.STORNO,
        Invoice.DocumentType.DOBROPIS,
    ):
        if not (svezi.razlog or "").strip() or not svezi.povezan_racun_id:
            raise PdfRenderNapaka(
                "KOREKCIJA_NEPOPOLNA", "Korekcija nima razloga ali izvirnega računa."
            )
        if not svezi.povezan_racun.stevilka:
            raise PdfRenderNapaka("KOREKCIJA_NEPOPOLNA", "Izvirni račun nima številke.")
    preveri_nespremenljiv_asset(svezi.izdajatelj_logotip_slika_pot)
    preveri_nespremenljiv_asset(svezi.izdajatelj_podpisnik_slika_pot)

    vrstice = tuple(InvoiceLine.objects.filter(invoice=svezi).order_by("pk"))
    if not vrstice:
        raise PdfRenderNapaka("VRSTICE_MANJKAJO", "Račun nima vrstic.")
    for vrstica in vrstice:
        _preveri_polja(vrstica, ("opis", "ddv_nacin_obracuna", "ddv_rekap_skupina"))
        if (
            vrstica.ddv_nacin_obracuna
            in (
                VatRegime.NacinObracuna.OPROSCENO,
                VatRegime.NacinObracuna.OBRNJENA_DAVCNA,
            )
            and not vrstica.ddv_klavzula.strip()
        ):
            raise PdfRenderNapaka(
                "KLAVZULA_MANJKA", "Vrstica nima obvezne DDV klavzule."
            )
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
        raise PdfRenderNapaka(
            "VSOTE_NESKLADNE", "Vsote računa se ne ujemajo z vrsticami."
        )
    if svezi.document_type == Invoice.DocumentType.ST:
        _preveri_st_posnetek(svezi, vrstice)
    elif (svezi.document_type in (Invoice.DocumentType.STORNO, Invoice.DocumentType.DOBROPIS)
            and svezi.povezan_racun.document_type == Invoice.DocumentType.ST
            and svezi.povezan_racun.lease_id is None):
        from invoicing.corrections import validate_st_correction_pdf

        try:
            validate_st_correction_pdf(svezi, vrstice)
        except ValidationError as napaka:
            raise PdfRenderNapaka("ST_REFERENCA_NEVELJAVNA", str(napaka)) from napaka
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
    lease = (
        Lease.objects.select_related("tenant", "unit__property__company")
        .filter(pk=lease.pk)
        .first()
    )
    if lease is None:
        raise PdfRenderNapaka("POGODBA_NE_OBSTAJA", "Pogodba ne obstaja.")
    kandidat = izracunaj_kandidata(lease, obdobje_od, obdobje_do, datum_izdaje)
    if not kandidat.se_izda:
        raise PdfRenderNapaka(
            "VZOREC_NEVELJAVEN", kandidat.razlog_neizdaje or "Ni obračunljivih vrstic."
        )
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
