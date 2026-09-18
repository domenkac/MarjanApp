from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from itertools import pairwise

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Q

from companies.models import Company, NumberSequence, Signatory
from companies.services import dodeli_naslednjo_stevilko
from contracts.models import IndexationEvent, Lease, LeaseItem
from contracts.services import (
    dnevi_v_obdobju,
    efektivna_veljavnost_pogodbe,
    izracunaj_postavko,
)
from core.models import AuditLogEntry

from .models import Invoice, InvoiceLine


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


def _podpisniki_na_dan(company: Company, datum_izdaje: date):
    return Signatory.objects.filter(
        company=company, velja_od__lte=datum_izdaje
    ).filter(Q(velja_do__isnull=True) | Q(velja_do__gte=datum_izdaje))


def preveri_predpogoje_podjetja(
    company: Company, datum_izdaje: date
) -> list[Blokada]:
    blokade = []
    if not company.ddv_razporeditev_potrjena:
        blokade.append(
            Blokada("DDV_NEPOTRJEN", "DDV razporeditev enot še ni potrjena — Nastavitve")
        )
    podpisniki = list(_podpisniki_na_dan(company, datum_izdaje).order_by("pk")[:2])
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
    for polje, vrednost in spremembe.items():
        setattr(svezi, polje, vrednost)
    svezi.full_clean()
    svezi.save(update_fields=spremembe)
    return svezi


@transaction.atomic
def dodaj_vrstico(invoice: Invoice, **podatki) -> InvoiceLine:
    svezi = preveri_uredljiv_osnutek(invoice)
    if "invoice" in podatki or "invoice_id" in podatki:
        raise ValueError("Račun vrstice določa argument invoice.")
    vrstica = InvoiceLine(invoice=svezi, **podatki)
    vrstica.full_clean()
    vrstica.save()
    return vrstica


@transaction.atomic
def spremeni_vrstico(vrstica: InvoiceLine, **spremembe) -> InvoiceLine:
    sveza = InvoiceLine.objects.get(pk=vrstica.pk)
    preveri_uredljiv_osnutek(sveza.invoice)
    if not spremembe or not spremembe.keys() <= POSLOVNA_POLJA_VRSTICE:
        raise ValueError("Dovoljena so samo poslovna in posnetna polja vrstice.")
    for polje, vrednost in spremembe.items():
        setattr(sveza, polje, vrednost)
    sveza.full_clean()
    sveza.save(update_fields=spremembe)
    return sveza


@transaction.atomic
def izbrisi_vrstico(vrstica: InvoiceLine) -> None:
    sveza = InvoiceLine.objects.get(pk=vrstica.pk)
    preveri_uredljiv_osnutek(sveza.invoice)
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
    podpisnik = _podpisniki_na_dan(company, datum_izdaje).get()

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

    vrstice = list(
        InvoiceLine.objects.filter(invoice=invoice).values_list("osnova", "ddv_znesek")
    )
    if not vrstice:
        raise ValueError("NP osnutek nima vrstic.")

    invoice.osnova_skupaj = sum((osnova for osnova, _ in vrstice), Decimal("0.00"))
    invoice.ddv_skupaj = sum((ddv for _, ddv in vrstice), Decimal("0.00"))
    invoice.skupaj_z_ddv = invoice.osnova_skupaj + invoice.ddv_skupaj
    invoice.rok_placila = invoice.datum_izdaje + timedelta(
        days=invoice.lease.rok_placila_dni
    )
    zaporedna = dodeli_naslednjo_stevilko(
        invoice.company, NumberSequence.Tip.NP, invoice.datum_izdaje
    )
    invoice.stevilka = f"NP-{zaporedna:05d}"
    invoice.status = Invoice.Status.IZBRAN_ZA_POSILJANJE
    invoice.save(
        update_fields=[
            "stevilka",
            "osnova_skupaj",
            "ddv_skupaj",
            "skupaj_z_ddv",
            "rok_placila",
            "status",
        ]
    )
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
    """Primerja shranjene vrstice NP osnutka z zadnjim številčenim NP."""
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
            stevilka__isnull=False,
            obdobje_do__lt=osnutek.obdobje_od,
        )
        .exclude(stevilka="")
        .exclude(status=Invoice.Status.ARHIVIRAN_NI_IZDAN)
        .order_by("-obdobje_do", "-datum_izdaje", "-pk")
        .first()
    )
    if prejsnji is None:
        return DeltaPrimerjava(
            None, False, None, (DeltaRazlaga(None, "PRVA_IZDAJA", "Prva izdaja"),)
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
            (DeltaRazlaga(None, "BREZ_RAZLIKE", "Znesek je nespremenjen"),),
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
        tuple(razlage),
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
