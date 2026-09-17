from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.db import transaction

from catalogs.models import VatRegime
from core.models import AuditLogEntry
from core.pravila import zaokrozi
from invoicing.models import Invoice, InvoiceLine

from .models import IndexationEvent, Lease, LeaseItem, PoracunIndeksacije


@dataclass(frozen=True)
class IzracunPostavke:
    osnova: Decimal
    ddv_stopnja: Decimal
    ddv_znesek: Decimal
    skupaj: Decimal
    ddv_nacin_obracuna: str
    ddv_klavzula: str
    ddv_rekap_skupina: str
    je_sorazmerno: bool


def dnevi_v_obdobju(
    obdobje_od: date,
    obdobje_do: date,
    entiteta_zacetek: date | None,
    entiteta_konec: date | None,
) -> int:
    ef_zacetek = max(obdobje_od, entiteta_zacetek) if entiteta_zacetek else obdobje_od
    ef_konec = min(obdobje_do, entiteta_konec) if entiteta_konec else obdobje_do
    if ef_zacetek > ef_konec:
        return 0
    return (ef_konec - ef_zacetek).days + 1


def efektivna_veljavnost_pogodbe(pogodba: Lease) -> tuple[date, date | None]:
    konec = (
        pogodba.datum_konca
        if pogodba.status == Lease.Status.AKTIVNA
        else pogodba.datum_prenehanja
    )
    return pogodba.datum_zacetka, konec


def efektivna_veljavnost_postavke(postavka: LeaseItem) -> tuple[date, date | None]:
    zacetek_pogodbe, konec_pogodbe = efektivna_veljavnost_pogodbe(postavka.lease)
    zacetek = max(zacetek_pogodbe, postavka.datum_zacetka_postavke or zacetek_pogodbe)
    konec_postavke = postavka.datum_prenehanja_postavke
    if konec_pogodbe and konec_postavke:
        konec = min(konec_pogodbe, konec_postavke)
    else:
        konec = konec_pogodbe or konec_postavke
    return zacetek, konec


def izracunaj_osnovo(
    znesek_neto_mesecno: Decimal,
    dnevi_najema: int,
    dnevi_v_mesecu: int,
) -> Decimal | None:
    if dnevi_najema == dnevi_v_mesecu:
        return znesek_neto_mesecno
    if dnevi_najema == 0:
        return None
    return zaokrozi(
        znesek_neto_mesecno * Decimal(dnevi_najema) / Decimal(dnevi_v_mesecu)
    )


def izracunaj_postavko(
    postavka: LeaseItem, obdobje_od: date, obdobje_do: date
) -> IzracunPostavke | None:
    pogodba = postavka.lease
    if (
        postavka.arhivirana
        or pogodba.arhivirana
        or not pogodba.tenant.aktiven
        or not pogodba.unit.aktivna
        or not pogodba.unit.property.aktivna
        or not pogodba.unit.property.company.aktivno
    ):
        return None

    zacetek, konec = efektivna_veljavnost_postavke(postavka)
    dnevi_najema = dnevi_v_obdobju(obdobje_od, obdobje_do, zacetek, konec)
    dnevi_v_mesecu = (obdobje_do - obdobje_od).days + 1
    osnova = izracunaj_osnovo(postavka.znesek_neto_mesecno, dnevi_najema, dnevi_v_mesecu)
    if osnova is None:
        return None

    rezim = postavka.vat_regime
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

    return IzracunPostavke(
        osnova=osnova,
        ddv_stopnja=rezim.stopnja,
        ddv_znesek=ddv_znesek,
        skupaj=osnova + ddv_znesek,
        ddv_nacin_obracuna=rezim.nacin_obracuna,
        ddv_klavzula=rezim.klavzula,
        ddv_rekap_skupina=rezim.rekapitulacijska_skupina,
        je_sorazmerno=dnevi_najema != dnevi_v_mesecu,
    )


@dataclass(frozen=True)
class IndeksacijaPredlog:
    pogoj_izpolnjen: bool
    razlog: str
    stara_vrednost: Decimal | None = None
    nova_vrednost: Decimal | None = None
    odstotek_spremembe: Decimal | None = None


def izracunaj_indeksacijo(
    lease_item: LeaseItem, nov_indeks: Decimal, danes: date
) -> IndeksacijaPredlog:
    lease = lease_item.lease
    if not lease.ima_klavzulo_indeksacije:
        return IndeksacijaPredlog(False, "Pogodba nima klavzule o indeksaciji")

    zadnji = (
        IndexationEvent.objects.filter(lease_item=lease_item, izkljucen=False)
        .order_by("-datum_uskladitve")
        .first()
    )
    zadnji_indeks = zadnji.indeks_vrednost if zadnji else lease_item.indeks_ob_osnovi
    razmerje = nov_indeks / zadnji_indeks
    odstotek = (razmerje - 1) * 100

    if (
        lease.tip_pogoja_indeksacije == Lease.TipPogojaIndeksacije.PRAG_SPREMEMBE
        and abs(odstotek) < lease.prag_spremembe_odstotek
    ):
        return IndeksacijaPredlog(
            False,
            f"Sprememba {odstotek:.2f}% pod pragom {lease.prag_spremembe_odstotek}%",
        )

    nova_vrednost = zaokrozi(lease_item.znesek_neto_mesecno * razmerje)
    if (
        lease.tip_pogoja_indeksacije == Lease.TipPogojaIndeksacije.SAMO_NAVZGOR
        and nova_vrednost < lease_item.znesek_neto_mesecno
    ):
        return IndeksacijaPredlog(
            False, "Indeks padel — klavzula dovoljuje samo zvišanje"
        )

    if lease.tip_pogoja_indeksacije == Lease.TipPogojaIndeksacije.PRAG_SPREMEMBE:
        razlog = f"Sprememba {odstotek:.2f}% dosega prag {lease.prag_spremembe_odstotek}%"
    elif lease.tip_pogoja_indeksacije == Lease.TipPogojaIndeksacije.SAMO_NAVZGOR:
        razlog = "Nova vrednost ni nižja od stare"
    else:
        razlog = "Klavzula velja vedno"

    return IndeksacijaPredlog(
        pogoj_izpolnjen=True,
        razlog=razlog,
        stara_vrednost=lease_item.znesek_neto_mesecno,
        nova_vrednost=nova_vrednost,
        odstotek_spremembe=odstotek,
    )


def zaznaj_zamujen_poracun(
    indexation_event: IndexationEvent,
) -> list[PoracunIndeksacije]:
    ze_pokrita = PoracunIndeksacije.objects.filter(
        indexation_event=indexation_event
    ).values_list("prizadeto_obdobje_od", flat=True)
    prizadete = (
        InvoiceLine.objects.filter(
            lease_item=indexation_event.lease_item,
            invoice__obdobje_od__gte=indexation_event.datum_uskladitve,
            invoice__obdobje_od__lt=date.today().replace(day=1),  # noqa: DTZ011
            invoice__status__in=[
                Invoice.Status.IZBRAN_ZA_POSILJANJE,
                Invoice.Status.POSLAN,
            ],
        )
        .exclude(invoice__obdobje_od__in=ze_pokrita)
        .select_related("invoice")
    )
    razlika = indexation_event.nova_vrednost - indexation_event.stara_vrednost
    return [
        PoracunIndeksacije(
            indexation_event=indexation_event,
            original_invoice=line.invoice,
            prizadeto_obdobje_od=line.invoice.obdobje_od,
            prizadeto_obdobje_do=line.invoice.obdobje_do,
            znesek_poracuna=razlika,
            status=PoracunIndeksacije.Status.PREDLAGAN,
        )
        for line in prizadete
    ]


@transaction.atomic
def potrdi_indeksacijo(
    lease_item: LeaseItem, nov_indeks: Decimal, danes: date
) -> IndexationEvent:
    postavka = LeaseItem.objects.select_for_update().get(pk=lease_item.pk)
    predlog = izracunaj_indeksacijo(postavka, nov_indeks, danes)
    if not predlog.pogoj_izpolnjen:
        raise ValueError(predlog.razlog)

    event = IndexationEvent(
        lease_item=postavka,
        datum_uskladitve=date(danes.year, 2, 1),
        indeks_vrednost=nov_indeks,
        stara_vrednost=predlog.stara_vrednost,
        nova_vrednost=predlog.nova_vrednost,
    )
    event.full_clean()
    event.save()
    postavka.znesek_neto_mesecno = predlog.nova_vrednost
    postavka.full_clean()
    postavka.save(update_fields=["znesek_neto_mesecno"])

    for poracun in zaznaj_zamujen_poracun(event):
        poracun.full_clean()
        poracun.save()
    return event


@transaction.atomic
def razveljavi_indeksacijo(indexation_event: IndexationEvent) -> IndexationEvent:
    dogodek = IndexationEvent.objects.select_for_update().get(pk=indexation_event.pk)
    if dogodek.izkljucen:
        raise ValueError("Indeksacija je že razveljavljena.")

    if InvoiceLine.objects.filter(
        lease_item_id=dogodek.lease_item_id,
        invoice__datum_izdaje__gte=dogodek.datum_uskladitve,
        invoice__stevilka__isnull=False,
    ).exists():
        raise ValueError("Po uskladitvi je bil izdan račun za to postavko.")

    postavka = LeaseItem.objects.select_for_update().get(pk=dogodek.lease_item_id)
    prejsnja_vrednost = postavka.znesek_neto_mesecno
    postavka.znesek_neto_mesecno = dogodek.stara_vrednost
    postavka.full_clean()

    razlog = f"Razveljavljena indeksacija #{dogodek.pk}"
    dogodek.izkljucen = True
    dogodek.razlog_izkljucitve = razlog
    dogodek.full_clean()

    audit = AuditLogEntry(
        content_type=ContentType.objects.get_for_model(LeaseItem),
        object_id=postavka.pk,
        dejanje=AuditLogEntry.Dejanje.SPREMEMBA,
        polje="znesek_neto_mesecno",
        stara_vrednost=str(prejsnja_vrednost),
        nova_vrednost=str(dogodek.stara_vrednost),
        razlog=razlog,
    )
    audit.full_clean()

    postavka.save(update_fields=["znesek_neto_mesecno"])
    dogodek.save(update_fields=["izkljucen", "razlog_izkljucitve"])
    audit.save()
    return dogodek
