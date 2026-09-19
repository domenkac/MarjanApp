from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from itertools import pairwise

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q

from catalogs.models import VatRegime
from companies.models import Company, NumberSequence
from companies.services import (
    dodeli_naslednjo_stevilko,
    izberi_veljavnega_podpisnika,
    shrani_nespremenljiv_asset,
    veljavni_podpisniki_na_dan,
)
from contracts.models import IndexationEvent, Lease, LeaseItem
from contracts.services import (
    dnevi_v_obdobju,
    efektivna_veljavnost_pogodbe,
    izracunaj_postavko,
)
from core.models import AuditLogEntry
from core.pravila import zaokrozi

from .models import Invoice, InvoiceLine

ESLOG_KODE_DDV = {
    VatRegime.NacinObracuna.OBDAVCENO: "S",
    VatRegime.NacinObracuna.OPROSCENO: "E",
    VatRegime.NacinObracuna.OBRNJENA_DAVCNA: "AE",
    VatRegime.NacinObracuna.NI_PREDMET: "O",
}


def _preveri_eslog_kodo(nacin: str, koda: str, vir: str) -> str:
    pricakovana = ESLOG_KODE_DDV[nacin]
    if not koda:
        raise ValueError(f"{vir} nima eSLOG kode.")
    if koda != pricakovana:
        raise ValueError(
            f"{vir} ima napačno eSLOG kodo {koda}; pričakovana je {pricakovana}."
        )
    return koda

MIGRACIJSKI_RAZLOG = "Migracijski zapis – uvoz stanja pred prehodom na aplikacijo"


@transaction.atomic
def uvozi_migracijsko_stanje(
    lease: Lease,
    zadnje_zaracunano_obdobje_od: date,
    zadnje_zaracunano_obdobje_do: date,
) -> Invoice:
    if zadnje_zaracunano_obdobje_od > zadnje_zaracunano_obdobje_do:
        raise ValueError("Začetek obdobja mora biti pred koncem obdobja.")

    lease = Lease.objects.select_for_update().select_related(
        "unit__property__company"
    ).get(pk=lease.pk)
    if Invoice.objects.filter(
        lease=lease,
        document_type=Invoice.DocumentType.NP,
        status=Invoice.Status.ARHIVIRAN_NI_IZDAN,
        razlog_arhiviranja=MIGRACIJSKI_RAZLOG,
    ).exists():
        raise ValueError("Migracijsko stanje za pogodbo je že uvoženo.")

    invoice = Invoice.objects.create(
        company=lease.unit.property.company,
        document_type=Invoice.DocumentType.NP,
        lease=lease,
        status=Invoice.Status.ARHIVIRAN_NI_IZDAN,
        razlog_arhiviranja=MIGRACIJSKI_RAZLOG,
        datum_izdaje=zadnje_zaracunano_obdobje_od,
        obdobje_od=zadnje_zaracunano_obdobje_od,
        obdobje_do=zadnje_zaracunano_obdobje_do,
    )
    vrstice = []
    for item in LeaseItem.objects.filter(
        lease=lease, datum_prenehanja_postavke__isnull=True
    ).select_related("vat_regime"):
        item.full_clean()
        rezim = item.vat_regime
        osnova = item.znesek_neto_mesecno
        if rezim.nacin_obracuna == VatRegime.NacinObracuna.OBDAVCENO:
            ddv_znesek = zaokrozi(osnova * rezim.stopnja / Decimal(100))
        elif rezim.nacin_obracuna in {
            VatRegime.NacinObracuna.OPROSCENO,
            VatRegime.NacinObracuna.NI_PREDMET,
            VatRegime.NacinObracuna.OBRNJENA_DAVCNA,
        }:
            ddv_znesek = Decimal("0.00")
        else:
            raise ValueError(f"Neznan način obračuna DDV: {rezim.nacin_obracuna}")
        vrstica = InvoiceLine(
            invoice=invoice,
            lease=lease,
            lease_item=item,
            opis=item.naziv_postavke,
            osnova=osnova,
            ddv_stopnja=rezim.stopnja,
            ddv_znesek=ddv_znesek,
            skupaj=osnova + ddv_znesek,
            ddv_nacin_obracuna=rezim.nacin_obracuna,
            ddv_klavzula=rezim.klavzula,
            ddv_rekap_skupina=rezim.rekapitulacijska_skupina,
        )
        vrstica.full_clean()
        vrstice.append(vrstica)
    InvoiceLine.objects.bulk_create(vrstice)
    return invoice


@dataclass(frozen=True)
class PripravaOsnutka:
    invoice: Invoice | None
    razlog_neizdaje: str | None = None


@dataclass(frozen=True)
class Blokada:
    koda: str
    sporocilo: str


@dataclass(frozen=True)
class RacunKandidat:
    vrstice: tuple[InvoiceLine, ...]
    skupaj_z_ddv: Decimal
    se_izda: bool
    izjeme: tuple["DeltaRazlaga", ...]
    razlog_neizdaje: str | None = None


def preveri_predpogoje_podjetja(
    company: Company, datum_izdaje: date
) -> list[Blokada]:
    blokade = []
    if not company.ddv_razporeditev_potrjena:
        blokade.append(
            Blokada("DDV_NEPOTRJEN", "DDV razporeditev enot še ni potrjena — Nastavitve")
        )
    podpisniki = list(veljavni_podpisniki_na_dan(company, datum_izdaje).order_by("pk")[:2])
    if not podpisniki:
        blokade.append(
            Blokada(
                "PODPISNIK_MANJKA",
                f"Ni veljavnega podpisnika za {company.naziv} na {datum_izdaje}",
            )
        )
    elif len(podpisniki) > 1:
        blokade.append(
            Blokada(
                "PODPISNIK_PREKRIVANJE",
                f"Več veljavnih podpisnikov za {company.naziv} na {datum_izdaje}; "
                "popravite obdobja veljavnosti",
            )
        )
    return blokade


POSLOVNA_POLJA_RACUNA = {
    "company",
    "document_type",
    "lease",
    "datum_izdaje",
    "obdobje_od",
    "obdobje_do",
    "rok_placila",
    "prejemnik_naziv",
    "prejemnik_tip",
    "prejemnik_naslov",
    "prejemnik_davcna_stevilka",
    "prejemnik_maticna_stevilka",
    "izdajatelj_naziv",
    "izdajatelj_naslov",
    "izdajatelj_davcna_stevilka",
    "izdajatelj_maticna_stevilka",
    "izdajatelj_trr",
    "izdajatelj_podpisnik_ime",
    "izdajatelj_podpisnik_funkcija",
    "izdajatelj_podpisnik_slika_pot",
    "oznaka_pogodbe",
    "objekt_naziv",
    "povezan_racun",
    "razlog",
    "razlog_arhiviranja",
    "osnova_skupaj",
    "ddv_skupaj",
    "skupaj_z_ddv",
}
POSLOVNA_POLJA_VRSTICE = {
    field.name for field in InvoiceLine._meta.concrete_fields
} - {"id", "invoice"}


def preveri_uredljiv_osnutek(invoice: Invoice) -> Invoice:
    """Vrne svež račun; spremembe se nato izvedejo v isti transakciji."""
    svezi = Invoice.objects.get(pk=invoice.pk)
    if svezi.status != Invoice.Status.OSNUTEK or svezi.stevilka is not None:
        raise ValueError("Račun ni neoštevilčen osnutek.")
    return svezi


@transaction.atomic
def spremeni_podatke_racuna(invoice: Invoice, **spremembe) -> Invoice:
    svezi = preveri_uredljiv_osnutek(invoice)
    if not spremembe or not spremembe.keys() <= POSLOVNA_POLJA_RACUNA:
        raise ValueError("Dovoljena so samo poslovna in posnetna polja računa.")
    if svezi.document_type in (
        Invoice.DocumentType.STORNO, Invoice.DocumentType.DOBROPIS
    ) and not spremembe.keys() <= {"datum_izdaje", "razlog"}:
        raise ValueError("Korekcijski osnutek dovoljuje samo datum izdaje in razlog.")
    for polje, vrednost in spremembe.items():
        setattr(svezi, polje, vrednost)
    svezi.full_clean()
    if (
        "document_type" in spremembe
        and svezi.document_type in (
            Invoice.DocumentType.STORNO,
            Invoice.DocumentType.DOBROPIS,
        )
    ):
        raise ValueError("Korekcijski osnutek se ustvari z namensko storitvijo.")
    svezi.save(update_fields=spremembe)
    return svezi


@transaction.atomic
def dodaj_vrstico(invoice: Invoice, **podatki) -> InvoiceLine:
    svezi = preveri_uredljiv_osnutek(invoice)
    if "invoice" in podatki or "invoice_id" in podatki:
        raise ValueError("Račun vrstice določa argument invoice.")
    if svezi.document_type == Invoice.DocumentType.STORNO:
        raise ValueError("Vrstic storna ni dovoljeno ročno spreminjati.")
    vrstica = InvoiceLine(invoice=svezi, **podatki)
    if svezi.document_type == Invoice.DocumentType.DOBROPIS:
        _preveri_rocno_vrstico_dobropisa(vrstica)
    vrstica.full_clean()
    vrstica.save()
    return vrstica


@transaction.atomic
def spremeni_vrstico(vrstica: InvoiceLine, **spremembe) -> InvoiceLine:
    sveza = InvoiceLine.objects.get(pk=vrstica.pk)
    invoice = preveri_uredljiv_osnutek(sveza.invoice)
    if invoice.document_type == Invoice.DocumentType.STORNO:
        raise ValueError("Vrstic storna ni dovoljeno ročno spreminjati.")
    if not spremembe or not spremembe.keys() <= POSLOVNA_POLJA_VRSTICE:
        raise ValueError("Dovoljena so samo poslovna in posnetna polja vrstice.")
    for polje, vrednost in spremembe.items():
        setattr(sveza, polje, vrednost)
    if invoice.document_type == Invoice.DocumentType.DOBROPIS:
        _preveri_rocno_vrstico_dobropisa(sveza)
    sveza.full_clean()
    sveza.save(update_fields=spremembe)
    return sveza


@transaction.atomic
def izbrisi_vrstico(vrstica: InvoiceLine) -> None:
    sveza = InvoiceLine.objects.get(pk=vrstica.pk)
    invoice = preveri_uredljiv_osnutek(sveza.invoice)
    if invoice.document_type == Invoice.DocumentType.STORNO:
        raise ValueError("Vrstic storna ni dovoljeno ročno spreminjati.")
    sveza.delete()


@transaction.atomic
def izbrisi_racun(invoice: Invoice) -> None:
    preveri_uredljiv_osnutek(invoice).delete()


@transaction.atomic
def pripravi_osnutek_np(
    lease: Lease, obdobje_od: date, obdobje_do: date, datum_izdaje: date
) -> PripravaOsnutka:
    kandidat = izracunaj_kandidata(lease, obdobje_od, obdobje_do, datum_izdaje)
    if not kandidat.se_izda:
        if kandidat.izjeme:
            raise ValueError(kandidat.razlog_neizdaje)
        return PripravaOsnutka(None, kandidat.razlog_neizdaje)

    lease = kandidat.vrstice[0].lease
    tenant = lease.tenant
    unit = lease.unit
    property = unit.property
    company = property.company
    podpisnik = izberi_veljavnega_podpisnika(company, datum_izdaje)

    invoice = Invoice.objects.create(
        company=company,
        lease=lease,
        document_type=Invoice.DocumentType.NP,
        datum_izdaje=datum_izdaje,
        obdobje_od=obdobje_od,
        obdobje_do=obdobje_do,
        status=Invoice.Status.OSNUTEK,
        prejemnik_naziv=tenant.naziv,
        prejemnik_naslov=tenant.naslov,
        prejemnik_davcna_stevilka=tenant.davcna_stevilka,
        prejemnik_maticna_stevilka=tenant.maticna_stevilka,
        izdajatelj_naziv=company.naziv,
        izdajatelj_naslov=company.naslov,
        izdajatelj_davcna_stevilka=company.davcna_stevilka,
        izdajatelj_maticna_stevilka=company.maticna_stevilka,
        izdajatelj_trr=company.trr,
        izdajatelj_podpisnik_ime=podpisnik.ime_priimek,
        izdajatelj_podpisnik_funkcija=podpisnik.funkcija,
        izdajatelj_podpisnik_slika_pot=podpisnik.podpis_slika.name,
        oznaka_pogodbe=lease.oznaka_pogodbe,
        objekt_naziv=property.naziv,
    )
    vrstice = list(kandidat.vrstice)
    for vrstica in vrstice:
        vrstica.invoice = invoice
    InvoiceLine.objects.bulk_create(vrstice)
    return PripravaOsnutka(invoice)


@transaction.atomic
def potrdi_osnutek_np(osnutek: Invoice) -> Invoice:
    invoice = preveri_uredljiv_osnutek(osnutek)
    if invoice.document_type != Invoice.DocumentType.NP:
        raise ValueError("Potrditi je mogoče samo NP osnutek.")
    if invoice.lease_id is None:
        raise ValueError("NP osnutek nima pogodbe.")
    if invoice.company_id != invoice.lease.unit.property.company_id:
        raise ValueError("NP podjetje se ne ujema s pogodbo.")

    vrstice = list(
        InvoiceLine.objects.filter(invoice=invoice).select_related(
            "lease_item__vat_regime"
        )
    )
    if not vrstice:
        raise ValueError("NP osnutek nima vrstic.")
    for vrstica in vrstice:
        if (
            vrstica.lease_id != invoice.lease_id
            or vrstica.lease_item_id is None
            or vrstica.lease_item.lease_id != invoice.lease_id
        ):
            raise ValueError("NP vrstica mora imeti postavko iste pogodbe.")
        if vrstica.skupaj != vrstica.osnova + vrstica.ddv_znesek:
            raise ValueError("NP vrstica: vsota osnove in DDV se ne ujema s skupaj.")

    invoice.prejemnik_tip = invoice.lease.tenant.tip
    for vrstica in vrstice:
        rezim = vrstica.lease_item.vat_regime
        vrstica.ddv_eslog_koda = _preveri_eslog_kodo(
            rezim.nacin_obracuna,
            rezim.eslog_koda,
            f"DDV režim {rezim.sifra}",
        )
    InvoiceLine.objects.bulk_update(vrstice, ["ddv_eslog_koda"])

    invoice.osnova_skupaj = sum((v.osnova for v in vrstice), Decimal("0.00"))
    invoice.ddv_skupaj = sum((v.ddv_znesek for v in vrstice), Decimal("0.00"))
    invoice.skupaj_z_ddv = invoice.osnova_skupaj + invoice.ddv_skupaj
    invoice.rok_placila = invoice.datum_izdaje + timedelta(
        days=invoice.lease.rok_placila_dni
    )
    invoice.izdajatelj_logotip_slika_pot = shrani_nespremenljiv_asset(
        invoice.company.logotip.name
    )
    invoice.izdajatelj_podpisnik_slika_pot = shrani_nespremenljiv_asset(
        invoice.izdajatelj_podpisnik_slika_pot
    )
    invoice.pdf_template_version = settings.PDF_TEMPLATE_VERSION
    zaporedna = dodeli_naslednjo_stevilko(
        invoice.company, NumberSequence.Tip.NP, invoice.datum_izdaje
    )
    invoice.stevilka = f"NP-{zaporedna:05d}"
    invoice.status = Invoice.Status.IZBRAN_ZA_POSILJANJE
    invoice.save(
        update_fields=[
            "stevilka",
            "prejemnik_tip",
            "izdajatelj_logotip_slika_pot",
            "izdajatelj_podpisnik_slika_pot",
            "pdf_template_version",
            "osnova_skupaj",
            "ddv_skupaj",
            "skupaj_z_ddv",
            "rok_placila",
            "status",
        ]
    )
    return invoice


_SNAPSHOT_POLJA_KOREKCIJE = (
    "prejemnik_naziv", "prejemnik_naslov",
    "prejemnik_davcna_stevilka", "prejemnik_maticna_stevilka",
    "prejemnik_tip",
    "izdajatelj_naziv", "izdajatelj_naslov",
    "izdajatelj_davcna_stevilka", "izdajatelj_maticna_stevilka",
    "izdajatelj_trr", "izdajatelj_podpisnik_ime",
    "izdajatelj_podpisnik_funkcija", "izdajatelj_podpisnik_slika_pot",
    "oznaka_pogodbe", "objekt_naziv",
)


def _preveri_izvirnik_korekcije(original: Invoice) -> Invoice:
    original = Invoice.objects.get(pk=original.pk)
    if not original.stevilka:
        raise ValueError("Izvirnik ni izdan.")
    if original.document_type not in (Invoice.DocumentType.NP, Invoice.DocumentType.ST):
        raise ValueError("Korekcija zahteva izvirni NP ali ST.")
    return original


def _preveri_rocno_vrstico_dobropisa(vrstica: InvoiceLine) -> None:
    napake = {}
    for polje in ("osnova", "ddv_stopnja", "ddv_znesek", "skupaj"):
        vrednost = getattr(vrstica, polje)
        if (
            not isinstance(vrednost, Decimal)
            or not vrednost.is_finite()
            or vrednost.as_tuple().exponent < -2
        ):
            napake[polje] = "Zahtevana je končna Decimal vrednost z največ dvema decimalkama."
    if not napake and vrstica.skupaj != vrstica.osnova + vrstica.ddv_znesek:
        napake["skupaj"] = "Skupaj mora biti vsota osnove in DDV."
    if napake:
        raise ValidationError(napake)


def _storno_vrstica(originalna: InvoiceLine, storno: Invoice) -> InvoiceLine:
    return InvoiceLine(
        invoice=storno,
        lease_id=originalna.lease_id,
        lease_item_id=originalna.lease_item_id,
        opis=originalna.opis,
        osnova=-originalna.osnova,
        ddv_stopnja=originalna.ddv_stopnja,
        ddv_znesek=-originalna.ddv_znesek,
        skupaj=-originalna.skupaj,
        ddv_nacin_obracuna=originalna.ddv_nacin_obracuna,
        ddv_klavzula=originalna.ddv_klavzula,
        ddv_rekap_skupina=originalna.ddv_rekap_skupina,
        ddv_eslog_koda=originalna.ddv_eslog_koda,
        je_sorazmerno=originalna.je_sorazmerno,
        razlog_rocnega_popravka=originalna.razlog_rocnega_popravka,
        izvirno_izracunana_vrednost=(
            -originalna.izvirno_izracunana_vrednost
            if originalna.izvirno_izracunana_vrednost is not None
            else None
        ),
    )


def _kljuc_storno_vrstice(vrstica: InvoiceLine) -> tuple:
    return (
        vrstica.lease_id, vrstica.lease_item_id, vrstica.opis,
        vrstica.osnova, vrstica.ddv_stopnja, vrstica.ddv_znesek,
        vrstica.skupaj, vrstica.ddv_nacin_obracuna, vrstica.ddv_klavzula,
        vrstica.ddv_rekap_skupina, vrstica.ddv_eslog_koda, vrstica.je_sorazmerno,
        vrstica.razlog_rocnega_popravka, vrstica.izvirno_izracunana_vrednost,
    )


@transaction.atomic
def pripravi_korekcijski_osnutek(
    original: Invoice, document_type: str, razlog: str, datum_izdaje: date
) -> Invoice:
    if document_type not in (
        Invoice.DocumentType.STORNO,
        Invoice.DocumentType.DOBROPIS,
    ):
        raise ValueError("Dovoljen je samo storno ali dobropis.")
    original = _preveri_izvirnik_korekcije(original)
    invoice = Invoice(
        company_id=original.company_id,
        lease_id=original.lease_id,
        document_type=document_type,
        povezan_racun=original,
        razlog=razlog,
        datum_izdaje=datum_izdaje,
        obdobje_od=original.obdobje_od,
        obdobje_do=original.obdobje_do,
        status=Invoice.Status.OSNUTEK,
        **{polje: getattr(original, polje) for polje in _SNAPSHOT_POLJA_KOREKCIJE},
    )
    invoice.full_clean()
    invoice.save()
    if document_type == Invoice.DocumentType.STORNO:
        vrstice = [
            _storno_vrstica(vrstica, invoice)
            for vrstica in InvoiceLine.objects.filter(invoice=original).order_by("pk")
        ]
        for vrstica in vrstice:
            vrstica.full_clean()
        InvoiceLine.objects.bulk_create(vrstice)
    return invoice


@transaction.atomic
def potrdi_korekcijski_osnutek(osnutek: Invoice) -> Invoice:
    invoice = preveri_uredljiv_osnutek(osnutek)
    if invoice.document_type not in (
        Invoice.DocumentType.STORNO,
        Invoice.DocumentType.DOBROPIS,
    ) or invoice.povezan_racun_id is None:
        raise ValueError("Račun ni korekcijski osnutek.")
    original = _preveri_izvirnik_korekcije(invoice.povezan_racun)
    if (
        invoice.company_id != original.company_id
        or invoice.lease_id != original.lease_id
        or invoice.obdobje_od != original.obdobje_od
        or invoice.obdobje_do != original.obdobje_do
        or any(
            getattr(invoice, polje) != getattr(original, polje)
            for polje in _SNAPSHOT_POLJA_KOREKCIJE
        )
    ):
        raise ValueError("Posnetek korekcije se ne ujema z izvirnikom.")
    invoice.full_clean()
    vrstice = list(InvoiceLine.objects.filter(invoice=invoice).order_by("pk"))
    if not vrstice:
        raise ValueError("Korekcijski osnutek nima vrstic.")
    if invoice.document_type == Invoice.DocumentType.STORNO:
        pricakovane = Counter(
            _kljuc_storno_vrstice(_storno_vrstica(v, invoice))
            for v in InvoiceLine.objects.filter(invoice=original)
        )
        dejanske = Counter(_kljuc_storno_vrstice(v) for v in vrstice)
        if pricakovane != dejanske:
            raise ValueError("Vrstice storna se ne ujemajo z izvirnikom.")
    else:
        for vrstica in vrstice:
            _preveri_rocno_vrstico_dobropisa(vrstica)
            vrstica.full_clean()

    for vrstica in vrstice:
        _preveri_eslog_kodo(
            vrstica.ddv_nacin_obracuna,
            vrstica.ddv_eslog_koda,
            "Vrstica dobropisa" if invoice.document_type == Invoice.DocumentType.DOBROPIS else "Izvirna vrstica storna",
        )

    invoice.osnova_skupaj = sum((v.osnova for v in vrstice), Decimal("0.00"))
    invoice.ddv_skupaj = sum((v.ddv_znesek for v in vrstice), Decimal("0.00"))
    invoice.skupaj_z_ddv = invoice.osnova_skupaj + invoice.ddv_skupaj
    tip = original.document_type
    invoice.izdajatelj_logotip_slika_pot = shrani_nespremenljiv_asset(
        invoice.company.logotip.name
    )
    invoice.izdajatelj_podpisnik_slika_pot = shrani_nespremenljiv_asset(
        invoice.izdajatelj_podpisnik_slika_pot
    )
    invoice.pdf_template_version = settings.PDF_TEMPLATE_VERSION
    zaporedna = dodeli_naslednjo_stevilko(invoice.company, tip, invoice.datum_izdaje)
    if tip == Invoice.DocumentType.NP:
        invoice.stevilka = f"NP-{zaporedna:05d}"
    else:
        invoice.stevilka = f"ST-{invoice.datum_izdaje.year}-{zaporedna:04d}"
    invoice.status = Invoice.Status.IZBRAN_ZA_POSILJANJE
    invoice.save(update_fields=[
        "osnova_skupaj", "ddv_skupaj", "skupaj_z_ddv", "stevilka", "status",
        "izdajatelj_logotip_slika_pot", "izdajatelj_podpisnik_slika_pot",
        "pdf_template_version",
    ])
    return invoice


@dataclass(frozen=True)
class DeltaRazlaga:
    lease_item_id: int | None
    koda: str
    opis: str


@dataclass(frozen=True)
class DeltaPrimerjava:
    prejsnji_racun_id: int | None
    obstaja_razlika: bool
    pojasnjena: bool | None
    razlage: tuple[DeltaRazlaga, ...]

    @property
    def ima_prejsnji_racun(self) -> bool:
        return self.prejsnji_racun_id is not None


def _razlozi_delta_postavke(
    lease_item_id: int | None,
    stare: list[InvoiceLine],
    nove: list[InvoiceLine],
    prejsnji: Invoice,
    osnutek: Invoice,
) -> DeltaRazlaga:
    neznano = DeltaRazlaga(lease_item_id, "VZROK_NEZNAN", "vzrok neznan – preveri ročno")
    if lease_item_id is None:
        return neznano
    postavka = LeaseItem.objects.filter(pk=lease_item_id, lease_id=osnutek.lease_id).first()
    if postavka is None:
        return neznano

    if not stare and nove:
        zacetek = postavka.datum_zacetka_postavke
        if zacetek and prejsnji.obdobje_do < zacetek <= osnutek.obdobje_do:
            return DeltaRazlaga(
                lease_item_id, "NOVA_POSTAVKA",
                f"Nova postavka na pogodbi (dodana {zacetek})",
            )
    if stare and not nove:
        konec = postavka.datum_prenehanja_postavke
        if konec and prejsnji.obdobje_od <= konec < osnutek.obdobje_od:
            return DeltaRazlaga(
                lease_item_id, "PRENEHANJE_POSTAVKE",
                f"Postavka preneha veljati {konec}",
            )

    if len(stare) == len(nove) == 1:
        stara, nova = stare[0], nove[0]
        isti_ddv = (
            stara.ddv_stopnja == nova.ddv_stopnja
            and stara.ddv_nacin_obracuna == nova.ddv_nacin_obracuna
        )
        dogodki = list(
            IndexationEvent.objects.filter(
                lease_item_id=lease_item_id,
                izkljucen=False,
                datum_uskladitve__gt=prejsnji.obdobje_do,
                datum_uskladitve__lte=osnutek.obdobje_do,
            ).order_by("datum_uskladitve", "pk")
        )
        if (
            dogodki
            and not stara.je_sorazmerno
            and not nova.je_sorazmerno
            and isti_ddv
            and stara.osnova == dogodki[0].stara_vrednost
            and nova.osnova == dogodki[-1].nova_vrednost
            and all(
                prvi.nova_vrednost == drugi.stara_vrednost
                for prvi, drugi in pairwise(dogodki)
            )
        ):
            return DeltaRazlaga(
                lease_item_id, "INDEKSACIJA",
                f"Indeksacija postavke z dne {dogodki[-1].datum_uskladitve}",
            )
        if stara.osnova == nova.osnova and not isti_ddv:
            return DeltaRazlaga(
                lease_item_id, "SPREMEMBA_DDV",
                f"Sprememba DDV: {stara.ddv_nacin_obracuna} {stara.ddv_stopnja}% → {nova.ddv_nacin_obracuna} {nova.ddv_stopnja}%",
            )

        revizije = AuditLogEntry.objects.filter(
            content_type=ContentType.objects.get_for_model(LeaseItem),
            object_id=lease_item_id,
            dejanje=AuditLogEntry.Dejanje.SPREMEMBA,
            polje="znesek_neto_mesecno",
            created_at__date__gte=prejsnji.datum_izdaje,
        ).order_by("-created_at", "-pk")
        for revizija in revizije:
            try:
                ujema_se = (
                    Decimal(revizija.stara_vrednost) == stara.osnova
                    and Decimal(revizija.nova_vrednost) == nova.osnova
                )
            except InvalidOperation:
                continue
            if ujema_se and isti_ddv:
                return DeltaRazlaga(
                    lease_item_id, "ROCNI_POPRAVEK",
                    f"Ročni popravek zneska: {revizija.razlog}",
                )
    return neznano


def primerjaj_delta_np(osnutek: Invoice) -> DeltaPrimerjava:
    """Primerja NP osnutek z zadnjim izdanim ali migracijskim stanjem."""
    osnutek = Invoice.objects.get(pk=osnutek.pk)
    if (
        osnutek.document_type != Invoice.DocumentType.NP
        or osnutek.status != Invoice.Status.OSNUTEK
        or osnutek.stevilka is not None
        or osnutek.lease_id is None
    ):
        raise ValueError("Primerjati je mogoče samo neoštevilčen NP osnutek.")

    return _primerjaj_delta_np(
        osnutek, list(InvoiceLine.objects.filter(invoice=osnutek))
    )


def _primerjaj_delta_np(
    osnutek: Invoice, nove_vrstice: tuple[InvoiceLine, ...] | list[InvoiceLine]
) -> DeltaPrimerjava:
    prejsnji = (
        Invoice.objects.filter(
            company_id=osnutek.company_id,
            lease_id=osnutek.lease_id,
            document_type=Invoice.DocumentType.NP,
            obdobje_do__lt=osnutek.obdobje_od,
        )
        .filter(
            (
                Q(stevilka__isnull=False)
                & ~Q(stevilka="")
                & ~Q(status=Invoice.Status.ARHIVIRAN_NI_IZDAN)
            )
            | Q(
                stevilka__isnull=True,
                status=Invoice.Status.ARHIVIRAN_NI_IZDAN,
                razlog_arhiviranja=MIGRACIJSKI_RAZLOG,
            )
        )
        .order_by("-obdobje_do", "-datum_izdaje", "-pk")
        .first()
    )
    if prejsnji is None:
        return DeltaPrimerjava(
            None, False, None, (DeltaRazlaga(None, "PRVA_IZDAJA", "Prva izdaja"),)
        )

    opozorila = (
        (DeltaRazlaga(None, "PREJSNJI_MESEC_NI_IZDAN",
                      "Prejšnji mesec ni bil izdan"),)
        if prejsnji.obdobje_do + timedelta(days=1) < osnutek.obdobje_od
        else ()
    )
    stare_vrstice = list(InvoiceLine.objects.filter(invoice=prejsnji))
    stari_znesek = (
        prejsnji.skupaj_z_ddv
        if prejsnji.skupaj_z_ddv is not None
        else sum((v.skupaj for v in stare_vrstice), Decimal("0.00"))
    )
    novi_znesek = sum((v.skupaj for v in nove_vrstice), Decimal("0.00"))
    if stari_znesek == novi_znesek:
        return DeltaPrimerjava(
            prejsnji.pk, False, None,
            (DeltaRazlaga(None, "BREZ_RAZLIKE", "Znesek je nespremenjen"),)
            + opozorila,
        )

    stare_po_postavki = {}
    nove_po_postavki = {}
    for vrstica in stare_vrstice:
        stare_po_postavki.setdefault(vrstica.lease_item_id, []).append(vrstica)
    for vrstica in nove_vrstice:
        nove_po_postavki.setdefault(vrstica.lease_item_id, []).append(vrstica)

    razlage = []
    pokrito_s_sorazmernostjo = False
    for lease_item_id in sorted(
        stare_po_postavki.keys() | nove_po_postavki.keys(),
        key=lambda value: (value is None, value or 0),
    ):
        stare = stare_po_postavki.get(lease_item_id, [])
        nove = nove_po_postavki.get(lease_item_id, [])
        if any(v.je_sorazmerno for v in (*stare, *nove)):
            pokrito_s_sorazmernostjo = True
            continue
        if sum((v.skupaj for v in stare), Decimal("0.00")) != sum(
            (v.skupaj for v in nove), Decimal("0.00")
        ):
            razlage.append(
                _razlozi_delta_postavke(lease_item_id, stare, nove, prejsnji, osnutek)
            )

    if stari_znesek != sum((v.skupaj for v in stare_vrstice), Decimal("0.00")):
        razlage.append(
            DeltaRazlaga(None, "VZROK_NEZNAN", "vzrok neznan – preveri ročno")
        )
    if not razlage and not pokrito_s_sorazmernostjo:
        razlage.append(
            DeltaRazlaga(None, "VZROK_NEZNAN", "vzrok neznan – preveri ročno")
        )
    return DeltaPrimerjava(
        prejsnji.pk, True,
        all(razlaga.koda != "VZROK_NEZNAN" for razlaga in razlage),
        tuple(razlage) + opozorila,
    )



def _izracunaj_vrstice_np(
    lease: Lease, obdobje_od: date, obdobje_do: date
) -> tuple[InvoiceLine, ...]:
    vrstice = []
    postavke = LeaseItem.objects.filter(lease=lease).select_related(
        "vat_regime", "lease__tenant", "lease__unit__property__company"
    )
    for postavka in postavke:
        izracun = izracunaj_postavko(postavka, obdobje_od, obdobje_do)
        if izracun is None:
            continue
        postavka.full_clean()
        pogodba = postavka.lease
        unit = pogodba.unit
        property = unit.property
        vrstice.append(
            InvoiceLine(
                lease=pogodba,
                lease_item=postavka,
                opis=(
                    f"{postavka.naziv_postavke} za obdobje "
                    f"{obdobje_od:%d.%m.%Y} – {obdobje_do:%d.%m.%Y}\n"
                    f"{unit.oznaka}, {property.naziv} · "
                    f"Pogodba št. {pogodba.oznaka_pogodbe}"
                ),
                **vars(izracun),
            )
        )
    return tuple(vrstice)


def izracunaj_kandidata(
    lease: Lease, obdobje_od: date, obdobje_do: date, datum_izdaje: date
) -> RacunKandidat:
    if obdobje_od > obdobje_do:
        raise ValueError("Začetek obdobja mora biti pred koncem obdobja.")

    vrstice = _izracunaj_vrstice_np(lease, obdobje_od, obdobje_do)
    if not vrstice:
        zacetek, konec = efektivna_veljavnost_pogodbe(lease)
        razlog = (
            "Pogodba ne velja v tem obdobju"
            if dnevi_v_obdobju(obdobje_od, obdobje_do, zacetek, konec) == 0
            else "Pogodba nima obračunljivih postavk v tem obdobju"
        )
        return RacunKandidat((), Decimal("0.00"), False, (), razlog)

    lease = vrstice[0].lease
    company = lease.unit.property.company
    skupaj = sum((vrstica.skupaj for vrstica in vrstice), Decimal("0.00"))
    blokade = preveri_predpogoje_podjetja(company, datum_izdaje)
    if blokade:
        return RacunKandidat(
            vrstice, skupaj, False,
            tuple(DeltaRazlaga(None, b.koda, b.sporocilo) for b in blokade),
            blokade[0].sporocilo,
        )

    izjeme = [
        DeltaRazlaga(v.lease_item_id, "SORAZMERNO", "Sorazmerni obračun postavke")
        for v in vrstice if v.je_sorazmerno
    ]
    osnutek = Invoice(
        company=company,
        lease=lease,
        document_type=Invoice.DocumentType.NP,
        status=Invoice.Status.OSNUTEK,
        datum_izdaje=datum_izdaje,
        obdobje_od=obdobje_od,
        obdobje_do=obdobje_do,
    )
    delta = _primerjaj_delta_np(osnutek, vrstice)
    izjeme.extend(r for r in delta.razlage if r.koda != "BREZ_RAZLIKE")
    return RacunKandidat(vrstice, skupaj, True, tuple(izjeme))
