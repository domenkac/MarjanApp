from dataclasses import dataclass
from datetime import date

from django.db import transaction
from django.db.models import Q

from companies.models import Signatory
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