from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Q

from companies.models import NumberSequence, Signatory
from companies.services import dodeli_naslednjo_stevilko
from contracts.models import Lease, LeaseItem
from contracts.services import (
    dnevi_v_obdobju,
    efektivna_veljavnost_pogodbe,
    izracunaj_postavko,
)

from .models import Invoice, InvoiceLine


@dataclass(frozen=True)
class PripravaOsnutka:
    invoice: Invoice | None
    razlog_neizdaje: str | None = None


@transaction.atomic
def pripravi_osnutek_np(
    lease: Lease, obdobje_od: date, obdobje_do: date, datum_izdaje: date
) -> PripravaOsnutka:
    if obdobje_od > obdobje_do:
        raise ValueError("Začetek obdobja mora biti pred koncem obdobja.")

    vrstice = []
    postavke = LeaseItem.objects.filter(lease=lease).select_related(
        "vat_regime", "lease__tenant", "lease__unit__property__company"
    )
    for postavka in postavke:
        izracun = izracunaj_postavko(postavka, obdobje_od, obdobje_do)
        if izracun is not None:
            vrstice.append((postavka, izracun))

    if not vrstice:
        zacetek, konec = efektivna_veljavnost_pogodbe(lease)
        razlog = (
            "Pogodba ne velja v tem obdobju"
            if dnevi_v_obdobju(obdobje_od, obdobje_do, zacetek, konec) == 0
            else "Pogodba nima obračunljivih postavk v tem obdobju"
        )
        return PripravaOsnutka(None, razlog)

    lease = vrstice[0][0].lease
    tenant = lease.tenant
    unit = lease.unit
    property = unit.property
    company = property.company

    if not company.ddv_razporeditev_potrjena:
        raise ValueError("DDV razporeditev enot še ni potrjena — Nastavitve")

    podpisniki = list(
        Signatory.objects.filter(company=company, velja_od__lte=datum_izdaje)
        .filter(Q(velja_do__isnull=True) | Q(velja_do__gte=datum_izdaje))
        .order_by("pk")[:2]
    )
    if not podpisniki:
        raise ValueError(
            f"Ni veljavnega podpisnika za {company.naziv} na {datum_izdaje}"
        )
    if len(podpisniki) > 1:
        raise ValueError("Več veljavnih podpisnikov. Popravite obdobja veljavnosti.")
    podpisnik = podpisniki[0]

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
    InvoiceLine.objects.bulk_create(
        [
            InvoiceLine(
                invoice=invoice,
                lease=lease,
                lease_item=postavka,
                opis=(
                    f"{postavka.naziv_postavke} za obdobje "
                    f"{obdobje_od:%d.%m.%Y} – {obdobje_do:%d.%m.%Y}\n"
                    f"{unit.oznaka}, {property.naziv} · "
                    f"Pogodba št. {lease.oznaka_pogodbe}"
                ),
                **vars(izracun),
            )
            for postavka, izracun in vrstice
        ]
    )
    return PripravaOsnutka(invoice)


@transaction.atomic
def potrdi_osnutek_np(osnutek: Invoice) -> Invoice:
    invoice = Invoice.objects.get(pk=osnutek.pk)
    if invoice.document_type != Invoice.DocumentType.NP:
        raise ValueError("Potrditi je mogoče samo NP osnutek.")
    if invoice.status != Invoice.Status.OSNUTEK or invoice.stevilka is not None:
        raise ValueError("Račun ni neoštevilčen osnutek.")
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
