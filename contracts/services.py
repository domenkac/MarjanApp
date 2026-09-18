from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError

from catalogs.models import VatRegime
from companies.models import Company
from core.models import AuditLogEntry
from core.pravila import zaokrozi
from core.services import IzbrisOdlocitev, IzbrisRazlog, StanjeRazlog, StanjeRezultat
from invoicing.models import Invoice, InvoiceLine

from .models import (
    IndexationEvent,
    Lease,
    LeaseItem,
    PoracunIndeksacije,
    Property,
    RecipientContact,
    Tenant,
    Unit,
)


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
    osnova = izracunaj_osnovo(
        postavka.znesek_neto_mesecno, dnevi_najema, dnevi_v_mesecu
    )
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


_ROCNO_UREDLJIVA_POLJA_POSTAVKE = frozenset(
    {
        "znesek_neto_mesecno",
        "datum_zacetka_postavke",
        "datum_prenehanja_postavke",
    }
)


@transaction.atomic
def rocno_popravi_postavko(
    postavka: LeaseItem, *, razlog: str, **spremembe
) -> LeaseItem:
    if not isinstance(razlog, str) or not razlog.strip():
        raise ValidationError({"razlog": "Razlog ročnega popravka je obvezen."})
    if spremembe.keys() - _ROCNO_UREDLJIVA_POLJA_POSTAVKE:
        raise ValueError("Nedovoljena poslovna polja za ročni popravek postavke.")

    sveza = LeaseItem.objects.get(pk=postavka.pk)
    stare_vrednosti = {polje: getattr(sveza, polje) for polje in spremembe}
    for polje, vrednost in spremembe.items():
        polje_modela = LeaseItem._meta.get_field(polje)
        setattr(sveza, polje, polje_modela.to_python(vrednost))

    sveza.full_clean()
    spremenjena = [
        polje for polje, stara in stare_vrednosti.items()
        if stara != getattr(sveza, polje)
    ]
    if spremenjena:
        sveza.save(update_fields=spremenjena)
        sveza.refresh_from_db(fields=spremenjena)
        for polje in spremenjena:
            _audit(
                sveza, polje, stare_vrednosti[polje], getattr(sveza, polje), razlog
            ).save()
    return sveza


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
        razlog = (
            f"Sprememba {odstotek:.2f}% dosega prag {lease.prag_spremembe_odstotek}%"
        )
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
            invoice__document_type=Invoice.DocumentType.NP,
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


_LASTNI_TIPI = (Property, Unit, Tenant, Lease, LeaseItem)
_POLJA = {
    Company: ("aktivno", True),
    Property: ("aktivna", True),
    Unit: ("aktivna", True),
    Tenant: ("aktiven", True),
    Lease: ("arhivirana", False),
    LeaseItem: ("arhivirana", False),
}
_NADREJENI = {
    Company: (),
    Property: ("company",),
    Unit: ("property",),
    Tenant: (),
    Lease: ("unit", "tenant"),
    LeaseItem: ("lease",),
}


def _svezi_zapis(zapis):
    if type(zapis) not in _LASTNI_TIPI:
        raise TypeError("Nepodprt tip poslovnega zapisa.")
    return type(zapis).objects.filter(pk=zapis.pk).first() if zapis.pk else None


def _nadrejeni(zapis):
    for ime in _NADREJENI[type(zapis)]:
        nadrejeni = getattr(zapis, ime)
        yield nadrejeni
        yield from _nadrejeni(nadrejeni)


def _audit(zapis, polje, stara_vrednost, nova_vrednost, razlog):
    revizija = AuditLogEntry(
        content_type=ContentType.objects.get_for_model(type(zapis)),
        object_id=zapis.pk,
        dejanje=AuditLogEntry.Dejanje.SPREMEMBA,
        polje=polje,
        stara_vrednost=str(stara_vrednost),
        nova_vrednost=str(nova_vrednost),
        razlog=razlog,
    )
    revizija.full_clean()
    return revizija


def arhiviraj_poslovni_zapis(zapis) -> StanjeRezultat:
    with transaction.atomic():
        svezi = _svezi_zapis(zapis)
        if svezi is None:
            return StanjeRezultat(
                False, (StanjeRazlog("ZAPIS_NE_OBSTAJA", "Zapis ne obstaja."),)
            )

        polje, aktivna_vrednost = _POLJA[type(svezi)]
        if getattr(svezi, polje) != aktivna_vrednost:
            return StanjeRezultat(
                False, (StanjeRazlog("ZE_ARHIVIRAN", "Zapis je že arhiviran."),)
            )

        setattr(svezi, polje, not aktivna_vrednost)
        revizija = _audit(
            svezi,
            polje,
            aktivna_vrednost,
            not aktivna_vrednost,
            "Arhiviranje poslovnega zapisa",
        )
        svezi.save(update_fields=[polje])
        revizija.save()
        return StanjeRezultat(True)


def ponovno_aktiviraj_poslovni_zapis(zapis) -> StanjeRezultat:
    try:
        with transaction.atomic():
            svezi = _svezi_zapis(zapis)
            if svezi is None:
                return StanjeRezultat(
                    False, (StanjeRazlog("ZAPIS_NE_OBSTAJA", "Zapis ne obstaja."),)
                )

            polje, aktivna_vrednost = _POLJA[type(svezi)]
            if getattr(svezi, polje) == aktivna_vrednost:
                return StanjeRezultat(
                    False, (StanjeRazlog("NI_ARHIVIRAN", "Zapis ni arhiviran."),)
                )

            razlogi = []
            for nadrejeni in _nadrejeni(svezi):
                nadrejeno_polje, nadrejena_aktivna = _POLJA[type(nadrejeni)]
                if getattr(nadrejeni, nadrejeno_polje) != nadrejena_aktivna:
                    razlogi.append(
                        StanjeRazlog(
                            "NEAKTIVEN_NADREJENI",
                            f"Nadrejeni zapis {type(nadrejeni).__name__} #{nadrejeni.pk} ni aktiven.",
                        )
                    )
            if razlogi:
                return StanjeRezultat(False, tuple(razlogi))

            setattr(svezi, polje, aktivna_vrednost)
            try:
                svezi.full_clean()
            except ValidationError as napaka:
                polja = getattr(napaka, "message_dict", {"__all__": napaka.messages})
                return StanjeRezultat(
                    False,
                    tuple(
                        StanjeRazlog("NEVELJAVEN_ZAPIS", f"{ime}: {sporocilo}")
                        for ime, sporocila in polja.items()
                        for sporocilo in sporocila
                    ),
                )
            except OSError:
                return StanjeRezultat(
                    False,
                    (
                        StanjeRazlog(
                            "NEVELJAVEN_ZAPIS", "Povezana datoteka ni dostopna."
                        ),
                    ),
                )

            revizija = _audit(
                svezi,
                polje,
                not aktivna_vrednost,
                aktivna_vrednost,
                "Ponovna aktivacija poslovnega zapisa",
            )
            svezi.save()
            revizija.save()
            return StanjeRezultat(True)
    except IntegrityError:
        return StanjeRezultat(
            False,
            (StanjeRazlog("OMEJITEV_PODATKOV", "Podatkovna omejitev ni izpolnjena."),),
        )


def odloci_o_fizicnem_izbrisu(zapis, *, izrecna_potrditev=False) -> IzbrisOdlocitev:
    svezi = _svezi_zapis(zapis)
    if svezi is None:
        return IzbrisOdlocitev(
            False,
            (
                IzbrisRazlog(
                    "ZAPIS_NE_OBSTAJA",
                    "Zapis ne obstaja.",
                    type(zapis).__name__,
                    0,
                ),
            ),
        )

    reference = {
        Property: ((Unit, "property_id", "PODREJENI_ZAPISI"),),
        Unit: ((Lease, "unit_id", "PODREJENI_ZAPISI"),),
        Tenant: ((Lease, "tenant_id", "PODREJENI_ZAPISI"),),
        Lease: (
            (LeaseItem, "lease_id", "PODREJENI_ZAPISI"),
            (RecipientContact, "lease_id", "PODREJENI_ZAPISI"),
            (Invoice, "lease_id", "POVEZAN_RACUN"),
            (InvoiceLine, "lease_id", "POVEZAN_RACUN"),
        ),
        LeaseItem: (
            (InvoiceLine, "lease_item_id", "POVEZAN_RACUN"),
            (IndexationEvent, "lease_item_id", "POSLOVNA_ZGODOVINA"),
        ),
    }
    razlogi = []
    for model, polje, koda in reference[type(svezi)]:
        stevilo = model.objects.filter(**{polje: svezi.pk}).count()
        if stevilo:
            razlogi.append(
                IzbrisRazlog(
                    koda,
                    f"Povezani zapisi {model.__name__} preprečujejo izbris.",
                    model.__name__,
                    stevilo,
                )
            )
    if type(svezi) is Lease:
        racuni = Invoice.objects.filter(lease_id=svezi.pk)
    elif type(svezi) is LeaseItem:
        racuni = Invoice.objects.filter(
            pk__in=InvoiceLine.objects.filter(lease_item_id=svezi.pk).values(
                "invoice_id"
            )
        )
    else:
        racuni = None
    if racuni is not None:
        stevilo = racuni.filter(stevilka__isnull=False).count()
        if stevilo:
            razlogi.append(
                IzbrisRazlog(
                    "ZAPIS_UPORABLJEN",
                    "Zapis je bil uporabljen na izdanem računu.",
                    "Invoice",
                    stevilo,
                )
            )
        stevilo = racuni.exclude(rendered_pdf_path="", rendered_eslog_path="").count()
        if stevilo:
            razlogi.append(
                IzbrisRazlog(
                    "POVEZAN_DOKUMENT",
                    "Ustvarjen dokument preprečuje izbris.",
                    "Invoice",
                    stevilo,
                )
            )
        stevilo = racuni.filter(status=Invoice.Status.POSLAN).count()
        if stevilo:
            razlogi.append(
                IzbrisRazlog(
                    "POVEZANO_POSILJANJE",
                    "Poslan račun preprečuje izbris.",
                    "Invoice",
                    stevilo,
                )
            )
    if type(svezi) is LeaseItem:
        stevilo = PoracunIndeksacije.objects.filter(
            indexation_event__lease_item_id=svezi.pk
        ).count()
        if stevilo:
            razlogi.append(
                IzbrisRazlog(
                    "POSLOVNA_ZGODOVINA",
                    "Poračun indeksacije preprečuje izbris.",
                    "PoracunIndeksacije",
                    stevilo,
                )
            )
    stevilo = AuditLogEntry.objects.filter(
        content_type=ContentType.objects.get_for_model(type(svezi)),
        object_id=svezi.pk,
    ).count()
    if stevilo:
        razlogi.append(
            IzbrisRazlog(
                "POSLOVNA_ZGODOVINA",
                "Revizijska zgodovina preprečuje izbris.",
                "AuditLogEntry",
                stevilo,
            )
        )
    if izrecna_potrditev is not True:
        razlogi.append(
            IzbrisRazlog(
                "POTRDITEV_MANJKA",
                "Uporabnik ni izrecno potrdil fizičnega izbrisa.",
                "potrditev",
                0,
            )
        )
    return IzbrisOdlocitev(not razlogi, tuple(razlogi), bool(razlogi))


def fizicno_izbrisi_poslovni_zapis(
    zapis, *, izrecna_potrditev=False
) -> IzbrisOdlocitev:
    odlocitev = odloci_o_fizicnem_izbrisu(zapis, izrecna_potrditev=izrecna_potrditev)
    if not odlocitev.dovoljeno:
        return odlocitev

    try:
        with transaction.atomic():
            odlocitev = odloci_o_fizicnem_izbrisu(
                zapis, izrecna_potrditev=izrecna_potrditev
            )
            if not odlocitev.dovoljeno:
                return odlocitev
            svezi = _svezi_zapis(zapis)
            revizija = AuditLogEntry(
                content_type=ContentType.objects.get_for_model(type(svezi)),
                object_id=svezi.pk,
                dejanje=AuditLogEntry.Dejanje.FIZICNI_IZBRIS,
                razlog="Uporabnik je izrecno potrdil fizični izbris neuporabljenega zapisa.",
            )
            revizija.full_clean()
            revizija.save()
            svezi.delete()
    except (ProtectedError, IntegrityError):
        odlocitev = odloci_o_fizicnem_izbrisu(
            zapis, izrecna_potrditev=izrecna_potrditev
        )
        if not odlocitev.dovoljeno:
            return odlocitev
        return IzbrisOdlocitev(
            False,
            (
                IzbrisRazlog(
                    "POSLOVNA_POVEZAVA",
                    "Podatkovna povezava preprečuje izbris.",
                    "omejitev_podatkov",
                    1,
                ),
            ),
            True,
        )
    return IzbrisOdlocitev(True)
