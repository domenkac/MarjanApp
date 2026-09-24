import hashlib
import json
from calendar import monthrange
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from itertools import pairwise

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from catalogs.models import VatRegime
from companies.models import Company, NumberSequence
from companies.services import (
    dodeli_naslednjo_stevilko,
    izberi_veljavnega_podpisnika,
    shrani_nespremenljiv_asset,
    veljavna_potrditev_ddv,
    veljavna_potrditev_vzorca,
    veljavni_podpisniki_na_dan,
)
from contracts.models import IndexationEvent, Lease, LeaseItem, RecipientContact
from contracts.services import (
    dnevi_v_obdobju,
    efektivna_veljavnost_pogodbe,
    efektivna_veljavnost_postavke,
    izracunaj_postavko,
)
from core.eslog_konfiguracija import razresi_eslog_konfiguracijo
from core.models import AuditLogEntry
from core.pravila import zaokrozi
from core.validators import (
    normaliziraj_iban,
    preveri_davcno_stevilko,
    preveri_iban,
    preveri_kodo_drzave,
    preveri_maticno_stevilko,
)

from .models import (
    Invoice,
    InvoiceLine,
    MonthlyBatch,
    MonthlyCandidateReview,
    MonthlyCycle,
)

ESLOG_KODE_DDV = {
    VatRegime.NacinObracuna.OBDAVCENO: "S",
    VatRegime.NacinObracuna.OPROSCENO: "E",
    VatRegime.NacinObracuna.OBRNJENA_DAVCNA: "AE",
    VatRegime.NacinObracuna.NI_PREDMET: "O",
}


class MesecniCikelNapaka(Exception):
    pass


class AktivniMesecniCikelObstaja(MesecniCikelNapaka):
    pass


class ZakljucenoObdobje(MesecniCikelNapaka):
    pass


class NiAktivnihPodjetij(MesecniCikelNapaka):
    pass


class KonfliktPodatkovPrvegaKoraka(MesecniCikelNapaka):
    pass


class NeveljavenPrehodMesecnegaCikla(MesecniCikelNapaka):
    pass


def normaliziraj_obdobje(obdobje: date) -> date:
    return obdobje.replace(day=1)


def nalozi_aktivni_mesecni_cikel() -> MonthlyCycle | None:
    return MonthlyCycle.objects.filter(status=MonthlyCycle.Status.V_TEKU).first()


def nalozi_mesecni_cikel(pk: int) -> MonthlyCycle:
    return MonthlyCycle.objects.get(pk=pk)


def _poisci_nadaljevanje(obdobje: date) -> MonthlyCycle | None:
    aktivni = nalozi_aktivni_mesecni_cikel()
    if aktivni and aktivni.obdobje != obdobje:
        raise AktivniMesecniCikelObstaja(
            "Drug mesečni obračun je že v teku. Najprej nadaljujte tega."
        )
    if aktivni:
        return aktivni
    obstojeci = MonthlyCycle.objects.filter(obdobje=obdobje).first()
    if obstojeci:
        raise ZakljucenoObdobje("Že zaključenega obdobja ni mogoče znova začeti.")
    return None


def _zacni_ali_nadaljuj_v_transakciji(
    obdobje: date, datum_izdaje: date
) -> MonthlyCycle:
    cycle = _poisci_nadaljevanje(obdobje)
    if cycle:
        return cycle
    podjetja = list(Company.objects.filter(aktivno=True))
    if not podjetja:
        raise NiAktivnihPodjetij(
            "Mesečnega obračuna ni mogoče začeti brez aktivnega podjetja."
        )
    cycle = MonthlyCycle.objects.create(
        obdobje=obdobje,
        datum_izdaje=datum_izdaje,
    )
    MonthlyBatch.objects.bulk_create(
        MonthlyBatch(cycle=cycle, company=company)
        for company in podjetja
    )
    return cycle


def _preveri_podatke_prvega_koraka(
    cycle: MonthlyCycle, datum_izdaje: date
) -> None:
    if cycle.datum_izdaje != datum_izdaje:
        raise KonfliktPodatkovPrvegaKoraka(
            "Datum izdaje se ne ujema z že shranjenim mesečnim obračunom."
        )


def _obnovi_zmagovalca(obdobje: date, datum_izdaje: date) -> MonthlyCycle:
    cycle = _poisci_nadaljevanje(obdobje)
    if cycle:
        _preveri_podatke_prvega_koraka(cycle, datum_izdaje)
        return cycle
    raise IntegrityError("Mesečnega cikla po konfliktu ni mogoče obnoviti.")


def zacni_ali_nadaljuj_mesecni_cikel(
    obdobje: date, datum_izdaje: date
) -> MonthlyCycle:
    obdobje = normaliziraj_obdobje(obdobje)
    try:
        with transaction.atomic():
            cycle = _zacni_ali_nadaljuj_v_transakciji(obdobje, datum_izdaje)
            _preveri_podatke_prvega_koraka(cycle, datum_izdaje)
            return cycle
    except IntegrityError:
        # Po neuspešni transakciji poizvedujemo zunaj njenega pokvarjenega stanja.
        return _obnovi_zmagovalca(obdobje, datum_izdaje)


def shrani_prvi_korak(obdobje: date, datum_izdaje: date) -> MonthlyCycle:
    obdobje = normaliziraj_obdobje(obdobje)
    try:
        with transaction.atomic():
            cycle = _zacni_ali_nadaljuj_v_transakciji(obdobje, datum_izdaje)
            if cycle.trenutni_korak >= MonthlyCycle.Korak.PREDHODNA_KONTROLA:
                _preveri_podatke_prvega_koraka(cycle, datum_izdaje)
                return cycle
            cycle.datum_izdaje = datum_izdaje
            cycle.trenutni_korak = MonthlyCycle.Korak.PREDHODNA_KONTROLA
            cycle.save(update_fields=["datum_izdaje", "trenutni_korak", "updated_at"])
            return cycle
    except IntegrityError:
        return _obnovi_zmagovalca(obdobje, datum_izdaje)


def premakni_mesecni_cikel(cycle: MonthlyCycle, korak: int) -> MonthlyCycle:
    if korak not in MonthlyCycle.Korak.values:
        raise NeveljavenPrehodMesecnegaCikla("Korak mora biti med 1 in 6.")
    with transaction.atomic():
        svezi = MonthlyCycle.objects.get(pk=cycle.pk)
        if svezi.status == MonthlyCycle.Status.ZAKLJUCEN:
            raise NeveljavenPrehodMesecnegaCikla(
                "Zaključenega mesečnega cikla ni mogoče premakniti."
            )
        if korak == svezi.trenutni_korak:
            return svezi
        if (
            svezi.trenutni_korak == MonthlyCycle.Korak.PAKETNI_PREGLED
            and korak == MonthlyCycle.Korak.POTRDITEV_PAKETA
        ):
            raise NeveljavenPrehodMesecnegaCikla(
                "Korak 5 zahteva potrditev in zamrznitev paketa."
            )
        if korak != svezi.trenutni_korak + 1:
            raise NeveljavenPrehodMesecnegaCikla(
                "Mesečni cikel se lahko premakne samo na naslednji korak."
            )
        svezi.trenutni_korak = korak
        svezi.save(update_fields=["trenutni_korak", "updated_at"])
        return svezi


def _preflight_issue(code, message, model, object_id, field=None, *, warning=False):
    return {
        "code": code,
        "severity": "WARNING" if warning else "BLOCKER",
        "message": message,
        "model": model,
        "object_id": object_id,
        "field": field,
    }


def _confirmation_issues(company):
    issues = []
    for result, missing_code, stale_code, label in (
        (
            veljavna_potrditev_ddv(company),
            "DDV_POTRDITEV_MANJKA",
            "DDV_POTRDITEV_ZASTARELA",
            "Potrditev nastavitev DDV",
        ),
        (
            veljavna_potrditev_vzorca(company),
            "VZOREC_POTRDITEV_MANJKA",
            "VZOREC_POTRDITEV_ZASTARELA",
            "Potrditev vzorca računa",
        ),
    ):
        if result.stanje == "missing":
            issues.append(
                _preflight_issue(
                    missing_code,
                    f"{label} manjka.",
                    "Company",
                    company.pk,
                )
            )
        elif result.stanje == "stale":
            issues.append(
                _preflight_issue(
                    stale_code,
                    f"{label} ni več veljavna za trenutno stanje.",
                    "Company",
                    company.pk,
                )
            )
    return issues


def _signatory_issues(company, issue_date):
    count = veljavni_podpisniki_na_dan(company, issue_date).count()
    if count == 0:
        return [
            _preflight_issue(
                "PODPISNIK_MANJKA",
                f"Za datum izdaje {issue_date:%d. %m. %Y} ni veljavnega podpisnika.",
                "Company",
                company.pk,
            )
        ]
    if count > 1:
        return [
            _preflight_issue(
                "PODPISNIK_PREKRIVANJE",
                "Za datum izdaje je veljavnih več podpisnikov.",
                "Company",
                company.pk,
            )
        ]
    return []


def _validation_issue(code, instance, field, message):
    return _preflight_issue(code, message, type(instance).__name__, instance.pk, field)


def _field_validation_issues(instance, fields, code):
    excluded = {field.name for field in instance._meta.fields} - set(fields)
    try:
        instance.clean_fields(exclude=excluded)
    except ValidationError as exc:
        return [
            _validation_issue(code, instance, field, str(messages[0]))
            for field, messages in exc.message_dict.items()
        ]
    return []


def _company_validation_issues(company):
    issues = _field_validation_issues(
        company,
        {"naziv", "davcna_stevilka", "maticna_stevilka", "naslov", "drzava_koda", "trr"},
        "PODJETJE_NEVELJAVNO",
    )
    for field, message in (
        ("naziv", "Manjka naziv podjetja."),
        ("naslov", "Manjka naslov podjetja."),
        ("davcna_stevilka", "Manjka davčna številka podjetja."),
        ("maticna_stevilka", "Manjka matična številka podjetja."),
        ("drzava_koda", "Manjka država podjetja."),
        ("trr", "Manjka račun podjetja."),
    ):
        if not str(getattr(company, field, "") or "").strip():
            issues.append(
                _validation_issue("PODJETJE_NEVELJAVNO", company, field, message)
            )
    if company.davcna_stevilka and not preveri_davcno_stevilko(
        company.davcna_stevilka
    ):
        issues.append(
            _validation_issue(
                "PODJETJE_NEVELJAVNO",
                company,
                "davcna_stevilka",
                "Davčna številka podjetja ni veljavna.",
            )
        )
    if company.maticna_stevilka and not preveri_maticno_stevilko(
        company.maticna_stevilka
    ):
        issues.append(
            _validation_issue(
                "PODJETJE_NEVELJAVNO",
                company,
                "maticna_stevilka",
                "Matična številka podjetja ni veljavna.",
            )
        )
    try:
        preveri_kodo_drzave(company.drzava_koda)
    except ValidationError as exc:
        issues.append(
            _validation_issue(
                "PODJETJE_NEVELJAVNO",
                company,
                "drzava_koda",
                str(exc.messages[0]),
            )
        )
    if company.trr and not preveri_iban(normaliziraj_iban(company.trr)):
        issues.append(
            _validation_issue(
                "PODJETJE_NEVELJAVNO",
                company,
                "trr",
                "IBAN podjetja ni veljaven.",
            )
        )
    return issues


def _tenant_validation_issues(tenant):
    issues = _field_validation_issues(
        tenant,
        {
            "tip", "naziv", "davcna_stevilka", "ddv_identifikacijska_stevilka",
            "maticna_stevilka", "naslov", "drzava_koda", "identificiran_za_ddv",
            "ddv_zavezanec_s_polnim_odbitkom",
        },
        "NAJEMNIK_NEVELJAVEN",
    )
    for field, message in (
        ("naziv", "Manjka naziv najemnika."),
        ("naslov", "Manjka naslov najemnika."),
        ("drzava_koda", "Manjka država najemnika."),
    ):
        if not str(getattr(tenant, field, "") or "").strip():
            issues.append(
                _validation_issue("NAJEMNIK_NEVELJAVEN", tenant, field, message)
            )
    try:
        preveri_kodo_drzave(tenant.drzava_koda)
    except ValidationError as exc:
        issues.append(
            _validation_issue(
                "NAJEMNIK_NEVELJAVEN",
                tenant,
                "drzava_koda",
                str(exc.messages[0]),
            )
        )
    tax_number = (tenant.davcna_stevilka or "").strip()
    registration_number = (tenant.maticna_stevilka or "").strip()
    if (tenant.tip == tenant.Tip.PRAVNA_OSEBA or tenant.identificiran_za_ddv) and not tax_number:
        issues.append(
            _validation_issue(
                "NAJEMNIK_NEVELJAVEN",
                tenant,
                "davcna_stevilka",
                "Davčna številka pravne osebe je obvezna.",
            )
        )
    if tenant.drzava_koda == "SI" and tax_number and not preveri_davcno_stevilko(
        tax_number
    ):
        issues.append(
            _validation_issue(
                "NAJEMNIK_NEVELJAVEN",
                tenant,
                "davcna_stevilka",
                "Davčna številka najemnika ni veljavna.",
            )
        )
    if (
        tenant.tip == tenant.Tip.PRAVNA_OSEBA
        and tenant.drzava_koda == "SI"
        and not registration_number
    ):
        issues.append(
            _validation_issue(
                "NAJEMNIK_NEVELJAVEN",
                tenant,
                "maticna_stevilka",
                "Matična številka slovenske pravne osebe je obvezna.",
            )
        )
    if (
        tenant.drzava_koda == "SI"
        and registration_number
        and not preveri_maticno_stevilko(registration_number)
    ):
        issues.append(
            _validation_issue(
                "NAJEMNIK_NEVELJAVEN",
                tenant,
                "maticna_stevilka",
                "Matična številka najemnika ni veljavna.",
            )
        )
    if (
        tenant.identificiran_za_ddv
        and tenant.drzava_koda != "SI"
        and not (tenant.ddv_identifikacijska_stevilka or "").strip()
    ):
        issues.append(
            _validation_issue(
                "NAJEMNIK_NEVELJAVEN",
                tenant,
                "ddv_identifikacijska_stevilka",
                "Tuji najemnik, identificiran za DDV, mora imeti DDV identifikacijsko številko.",
            )
        )
    if tenant.ddv_zavezanec_s_polnim_odbitkom and not tenant.identificiran_za_ddv:
        issues.append(
            _validation_issue(
                "NAJEMNIK_NEVELJAVEN",
                tenant,
                "ddv_zavezanec_s_polnim_odbitkom",
                "Polni odbitek DDV zahteva identifikacijo za DDV.",
            )
        )
    return issues


def _source_validation_issues(lease, items):
    issues = _field_validation_issues(
        lease.unit.property,
        {"naziv", "naslov"},
        "NEPREMICNINA_NEVELJAVNA",
    )
    issues.extend(
        _field_validation_issues(
            lease.unit, {"oznaka", "tip_enote", "povrsina_m2"}, "ENOTA_NEVELJAVNA"
        )
    )
    tenant = lease.tenant
    for instance, field, code, message in (
        (lease.unit.property, "naziv", "NEPREMICNINA_NEVELJAVNA", "Manjka naziv nepremičnine."),
        (lease.unit.property, "naslov", "NEPREMICNINA_NEVELJAVNA", "Manjka naslov nepremičnine."),
        (lease.unit, "oznaka", "ENOTA_NEVELJAVNA", "Manjka oznaka enote."),
        (lease, "oznaka_pogodbe", "POGODBA_NEVELJAVNA", "Manjka oznaka pogodbe."),
    ):
        if not str(getattr(instance, field, "") or "").strip():
            issues.append(_validation_issue(code, instance, field, message))

    try:
        issues.extend(
            _field_validation_issues(
                lease,
                {
                    "oznaka_pogodbe", "tip_trajanja", "datum_zacetka",
                    "datum_konca", "samodejno_podaljsanje",
                    "obdobje_podaljsanja_mesecev", "odpovedni_rok_mesecev",
                    "datum_odpovedi", "datum_prenehanja", "status",
                    "rok_placila_dni", "ima_klavzulo_indeksacije",
                    "opis_pogoja_indeksacije", "tip_pogoja_indeksacije",
                    "prag_spremembe_odstotek",
                },
                "POGODBA_NEVELJAVNA",
            )
        )
        lease.clean()
    except ValidationError as exc:
        for field, messages in exc.message_dict.items():
            issues.append(
                _validation_issue(
                    "POGODBA_NEVELJAVNA", lease, field, str(messages[0])
                )
            )

    issues.extend(_tenant_validation_issues(tenant))

    for item in items:
        issues.extend(
            _field_validation_issues(
                item,
                {
                    "naziv_postavke", "znesek_neto_mesecno", "referenca_listine",
                    "datum_reference", "datum_zacetka_postavke",
                    "datum_prenehanja_postavke", "indeks_ob_osnovi",
                    "datum_osnove_indeksacije", "osnovna_najemnina_ob_sklenitvi",
                },
                "POSTAVKA_NEVELJAVNA",
            )
        )
        if not item.naziv_postavke.strip():
            issues.append(
                _validation_issue(
                    "POSTAVKA_NEVELJAVNA",
                    item,
                    "naziv_postavke",
                    "Manjka naziv postavke.",
                )
            )
        if item.znesek_neto_mesecno <= 0:
            issues.append(
                _validation_issue(
                    "POSTAVKA_NEVELJAVNA",
                    item,
                    "znesek_neto_mesecno",
                    "Mesečni znesek postavke mora biti večji od nič.",
                )
            )
        if lease.ima_klavzulo_indeksacije:
            for field, message in (
                ("indeks_ob_osnovi", "Manjka izhodiščni indeks postavke."),
                ("datum_osnove_indeksacije", "Manjka datum osnove indeksacije."),
            ):
                if getattr(item, field) is None:
                    issues.append(
                        _validation_issue("POSTAVKA_NEVELJAVNA", item, field, message)
                    )

        regime = item.vat_regime
        if regime.nacin_obracuna not in VatRegime.NacinObracuna.values:
            issues.append(
                _validation_issue(
                    "DDV_REZIM_NEVELJAVEN",
                    regime,
                    "nacin_obracuna",
                    "Način obračuna DDV ni veljaven.",
                )
            )
        if regime.stopnja is None:
            issues.append(
                _validation_issue(
                    "DDV_REZIM_NEVELJAVEN",
                    regime,
                    "stopnja",
                    "Manjka stopnja DDV.",
                )
            )
        if (
            regime.nacin_obracuna
            in {VatRegime.NacinObracuna.OPROSCENO, VatRegime.NacinObracuna.OBRNJENA_DAVCNA}
            and not regime.klavzula.strip()
        ):
            issues.append(
                _validation_issue(
                    "DDV_REZIM_NEVELJAVEN",
                    regime,
                    "klavzula",
                    "Za izbrani način obračuna DDV manjka klavzula.",
                )
            )
        if not regime.rekapitulacijska_skupina.strip():
            issues.append(
                _validation_issue(
                    "DDV_REZIM_NEVELJAVEN",
                    regime,
                    "rekapitulacijska_skupina",
                    "Manjka rekapitulacijska skupina DDV.",
                )
            )
        if regime.zahteva_referenco:
            if not item.referenca_listine.strip():
                issues.append(
                    _validation_issue(
                        "REFERENCA_LISTINE_MANJKA",
                        item,
                        "referenca_listine",
                        "Manjka obvezna referenca listine.",
                    )
                )
            if item.datum_reference is None:
                issues.append(
                    _validation_issue(
                        "DATUM_REFERENCE_MANJKA",
                        item,
                        "datum_reference",
                        "Manjka datum obvezne reference.",
                    )
                )
    return issues


def _lease_warning_issues(lease, items, period_start, period_end):
    issues = []
    start, end = efektivna_veljavnost_pogodbe(lease)
    if start > period_start or (end is not None and end < period_end):
        issues.append(
            _preflight_issue(
                "SORAZMERNO_OBDOBJE_POGODBE",
                "Pogodba je v mesecu veljavna le del obdobja.",
                "Lease",
                lease.pk,
                warning=True,
            )
        )
    if lease.tip_trajanja == Lease.TipTrajanja.DOLOCEN_CAS and lease.datum_konca:
        if period_start <= lease.datum_konca <= period_end:
            code, message = "POGODBA_POTECE_V_MESECU", "Pogodba poteče v izbranem mesecu."
        elif period_end < lease.datum_konca <= period_end + timedelta(days=30):
            code, message = "POGODBA_POTECE_V_30_DNEH", "Pogodba poteče v naslednjih 30 dneh."
        else:
            code = None
        if code:
            issues.append(_preflight_issue(code, message, "Lease", lease.pk, warning=True))
    if lease.samodejno_podaljsanje and lease.datum_konca and period_start <= lease.datum_konca <= period_end:
        issues.append(
            _preflight_issue(
                "SAMODEJNO_PODALJSANJE",
                "Pogodba v mesecu doseže datum samodejnega podaljšanja.",
                "Lease",
                lease.pk,
                warning=True,
            )
        )
    if lease.status == Lease.Status.ODPOVEDANA and end and period_start <= end <= period_end:
        issues.append(
            _preflight_issue(
                "ODPOVEDANA_ZADNJE_OBDOBJE",
                "Odpovedana pogodba je v zadnjem obračunskem obdobju.",
                "Lease",
                lease.pk,
                warning=True,
            )
        )
    for item in items:
        item_start, item_end = efektivna_veljavnost_postavke(item)
        if item_start > period_start or (item_end is not None and item_end < period_end):
            issues.append(
                _preflight_issue(
                    "SORAZMERNO_OBDOBJE_POSTAVKE",
                    "Postavka je v mesecu veljavna le del obdobja.",
                    "LeaseItem",
                    item.pk,
                    warning=True,
                )
            )
    return issues


def _company_preflight_issues(company, cycle, period_end):
    issues = (
        _confirmation_issues(company)
        + _signatory_issues(company, cycle.datum_izdaje)
        + _company_validation_issues(company)
    )
    leases = Lease.objects.filter(
        unit__property__company=company,
        arhivirana=False,
        unit__property__aktivna=True,
        unit__aktivna=True,
        tenant__aktiven=True,
    ).select_related("tenant", "unit__property__company")
    for lease in leases.order_by("pk"):
        start, end = efektivna_veljavnost_pogodbe(lease)
        if not dnevi_v_obdobju(cycle.obdobje, period_end, start, end):
            continue
        items = []
        for item in LeaseItem.objects.filter(lease=lease, arhivirana=False).select_related(
            "vat_regime", "lease__tenant", "lease__unit__property__company"
        ).order_by("pk"):
            item_start, item_end = efektivna_veljavnost_postavke(item)
            if dnevi_v_obdobju(cycle.obdobje, period_end, item_start, item_end):
                items.append(item)
        issues.extend(_source_validation_issues(lease, items))
        issues.extend(_lease_warning_issues(lease, items, cycle.obdobje, period_end))
    seen = set()
    unique = []
    for issue in issues:
        identity = tuple(
            issue[key]
            for key in ("code", "severity", "model", "object_id", "field")
        )
        if identity not in seen:
            seen.add(identity)
            unique.append(issue)
    return unique


def preveri_predhodno_kontrolo(cycle: MonthlyCycle) -> list[MonthlyBatch]:
    cycle = MonthlyCycle.objects.get(pk=cycle.pk)
    if not (
        MonthlyCycle.Korak.PREDHODNA_KONTROLA
        <= cycle.trenutni_korak
        <= MonthlyCycle.Korak.PAKETNI_PREGLED
    ):
        raise NeveljavenPrehodMesecnegaCikla(
            "Predhodna kontrola je dovoljena samo v korakih 2 do 4."
        )
    period_end = cycle.obdobje.replace(
        day=monthrange(cycle.obdobje.year, cycle.obdobje.month)[1]
    )
    checked = []
    for batch in MonthlyBatch.objects.filter(cycle=cycle).select_related("company").order_by("company_id"):
        issues = _company_preflight_issues(batch.company, cycle, period_end)
        status = (
            MonthlyBatch.PreflightStatus.BLOCKED
            if any(issue["severity"] == "BLOCKER" for issue in issues)
            else MonthlyBatch.PreflightStatus.PASSED
        )
        updates = {
            "preflight_status": status,
            "preflight_checked_at": timezone.now(),
            "preflight_issues": issues,
        }
        if status == MonthlyBatch.PreflightStatus.BLOCKED:
            updates["calculation_status"] = (
                MonthlyBatch.CalculationStatus.NOT_CALCULATED
            )
        with transaction.atomic():
            MonthlyBatch.objects.filter(pk=batch.pk).update(
                **updates,
            )
        batch.refresh_from_db()
        checked.append(batch)
    return checked


def _razresi_eslog_snapshot(invoice: Invoice) -> dict:
    if invoice.lease_id is None:
        raise ValueError("Račun nima pogodbe za razrešitev eSLOG konfiguracije.")
    lease = Lease.objects.select_related("tenant", "unit__property__company").get(
        pk=invoice.lease_id
    )
    if lease.unit.property.company_id != invoice.company_id:
        raise ValueError("Podjetje računa se ne ujema s pogodbo.")
    return razresi_eslog_konfiguracijo(
        lease.unit.property.company.eslog_privzeti_neobvezni_segmenti,
        lease.tenant.eslog_neobvezni_segmenti_override,
        lease.tenant.eslog_neobvezna_polja,
    )


def _preveri_eslog_kodo(nacin: str, koda: str, vir: str) -> str:
    pricakovana = ESLOG_KODE_DDV[nacin]
    if not koda:
        raise ValueError(f"{vir} nima eSLOG kode.")
    if koda != pricakovana:
        raise ValueError(
            f"{vir} ima napačno eSLOG kodo {koda}; pričakovana je {pricakovana}."
        )
    return koda


def _preveri_mesanje_eslog_o(vrstice) -> None:
    kode = {vrstica.ddv_eslog_koda for vrstica in vrstice}
    if "O" in kode and "S" in kode:
        raise ValueError(
            "eSLOG račun ne sme združevati kategorije O in obdavčenih postavk."
        )


def _preveri_ddv_klavzule(vrstice) -> None:
    if any(
        vrstica.ddv_eslog_koda in {"E", "AE"}
        and not (vrstica.ddv_klavzula or "").strip()
        for vrstica in vrstice
    ):
        raise ValueError("Vrstica kategorije E ali AE nima obvezne DDV klavzule.")


MIGRACIJSKI_RAZLOG = "Migracijski zapis – uvoz stanja pred prehodom na aplikacijo"


@transaction.atomic
def uvozi_migracijsko_stanje(
    lease: Lease,
    zadnje_zaracunano_obdobje_od: date,
    zadnje_zaracunano_obdobje_do: date,
) -> Invoice:
    if zadnje_zaracunano_obdobje_od > zadnje_zaracunano_obdobje_do:
        raise ValueError("Začetek obdobja mora biti pred koncem obdobja.")

    lease = Lease.objects.select_related("unit__property__company").get(pk=lease.pk)
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


def preveri_predpogoje_podjetja(company: Company, datum_izdaje: date) -> list[Blokada]:
    blokade = []
    if not company.ddv_razporeditev_potrjena:
        blokade.append(
            Blokada(
                "DDV_NEPOTRJEN", "DDV razporeditev enot še ni potrjena — Nastavitve"
            )
        )
    podpisniki = list(
        veljavni_podpisniki_na_dan(company, datum_izdaje).order_by("pk")[:2]
    )
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
    "prejemnik_ddv_identifikacijska_stevilka",
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
POSLOVNA_POLJA_VRSTICE = {field.name for field in InvoiceLine._meta.concrete_fields} - {
    "id",
    "invoice",
}


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
        Invoice.DocumentType.STORNO,
        Invoice.DocumentType.DOBROPIS,
    ) and not spremembe.keys() <= {"datum_izdaje", "razlog"}:
        raise ValueError("Korekcijski osnutek dovoljuje samo datum izdaje in razlog.")
    for polje, vrednost in spremembe.items():
        setattr(svezi, polje, vrednost)
    svezi.full_clean()
    if "document_type" in spremembe and svezi.document_type in (
        Invoice.DocumentType.STORNO,
        Invoice.DocumentType.DOBROPIS,
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
        prejemnik_tip=tenant.tip,
        prejemnik_naslov=tenant.naslov,
        prejemnik_drzava_koda=tenant.drzava_koda,
        prejemnik_identificiran_za_ddv=tenant.identificiran_za_ddv,
        prejemnik_davcna_stevilka=tenant.davcna_stevilka,
        prejemnik_ddv_identifikacijska_stevilka=(
            tenant.ddv_identifikacijska_stevilka
        ),
        prejemnik_maticna_stevilka=tenant.maticna_stevilka,
        izdajatelj_naziv=company.naziv,
        izdajatelj_naslov=company.naslov,
        izdajatelj_drzava_koda=company.drzava_koda,
        izdajatelj_identificiran_za_ddv=company.ddv_zavezanec,
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
def _potrdi_osnutek_np(
    osnutek: Invoice,
    *,
    podpisnik_id: int,
    logo_vir: str,
    logo_pot: str,
    podpis_vir: str,
    podpis_pot: str,
) -> Invoice:
    invoice = preveri_uredljiv_osnutek(osnutek)
    if invoice.document_type != Invoice.DocumentType.NP:
        raise ValueError("Potrditi je mogoče samo NP osnutek.")
    if invoice.lease_id is None:
        raise ValueError("NP osnutek nima pogodbe.")
    if invoice.company_id != invoice.lease.unit.property.company_id:
        raise ValueError("NP podjetje se ne ujema s pogodbo.")
    podpisnik = izberi_veljavnega_podpisnika(
        invoice.company, invoice.datum_izdaje
    )
    if podpisnik.pk != podpisnik_id or podpisnik.podpis_slika.name != podpis_vir:
        raise ValueError("Podpisnik se je med potrditvijo spremenil.")

    invoice.eslog_konfiguracija_snapshot = _razresi_eslog_snapshot(invoice)

    vrstice = list(InvoiceLine.objects.filter(invoice=invoice))
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

    for vrstica in vrstice:
        _preveri_eslog_kodo(
            vrstica.ddv_nacin_obracuna,
            vrstica.ddv_eslog_koda,
            "Snapshot vrstice",
        )
    _preveri_ddv_klavzule(vrstice)
    _preveri_mesanje_eslog_o(vrstice)

    invoice.osnova_skupaj = sum((v.osnova for v in vrstice), Decimal("0.00"))
    invoice.ddv_skupaj = sum((v.ddv_znesek for v in vrstice), Decimal("0.00"))
    invoice.skupaj_z_ddv = invoice.osnova_skupaj + invoice.ddv_skupaj
    invoice.rok_placila = invoice.datum_izdaje + timedelta(
        days=invoice.lease.rok_placila_dni
    )
    if invoice.company.logotip.name != logo_vir:
        raise ValueError("Logotip podjetja se je med potrditvijo spremenil.")
    invoice.izdajatelj_podpisnik_ime = podpisnik.ime_priimek
    invoice.izdajatelj_podpisnik_funkcija = podpisnik.funkcija
    invoice.izdajatelj_logotip_slika_pot = logo_pot
    invoice.izdajatelj_podpisnik_slika_pot = podpis_pot
    invoice.pdf_template_version = settings.PDF_TEMPLATE_VERSION
    zaporedna = dodeli_naslednjo_stevilko(
        invoice.company, NumberSequence.Tip.NP, invoice.datum_izdaje
    )
    invoice.stevilka = f"NP-{zaporedna:05d}"
    invoice.status = Invoice.Status.IZBRAN_ZA_POSILJANJE
    invoice.save(
        update_fields=[
            "stevilka",
            "izdajatelj_podpisnik_ime",
            "izdajatelj_podpisnik_funkcija",
            "izdajatelj_logotip_slika_pot",
            "izdajatelj_podpisnik_slika_pot",
            "pdf_template_version",
            "osnova_skupaj",
            "ddv_skupaj",
            "skupaj_z_ddv",
            "rok_placila",
            "status",
            "eslog_konfiguracija_snapshot",
        ]
    )
    return invoice


def potrdi_osnutek_np(osnutek: Invoice) -> Invoice:
    invoice = preveri_uredljiv_osnutek(osnutek)
    if (
        invoice.lease_id is None
        or invoice.company_id != invoice.lease.unit.property.company_id
    ):
        raise ValueError("NP podjetje se ne ujema s pogodbo.")
    podpisnik = izberi_veljavnega_podpisnika(
        invoice.company, invoice.datum_izdaje
    )
    logo_vir = invoice.company.logotip.name
    podpis_vir = podpisnik.podpis_slika.name
    logo_pot = shrani_nespremenljiv_asset(logo_vir)
    podpis_pot = shrani_nespremenljiv_asset(podpis_vir)
    return _potrdi_osnutek_np(
        osnutek,
        podpisnik_id=podpisnik.pk,
        logo_vir=logo_vir,
        logo_pot=logo_pot,
        podpis_vir=podpis_vir,
        podpis_pot=podpis_pot,
    )


_SNAPSHOT_POLJA_KOREKCIJE = (
    "prejemnik_naziv",
    "prejemnik_naslov",
    "prejemnik_davcna_stevilka",
    "prejemnik_ddv_identifikacijska_stevilka",
    "prejemnik_maticna_stevilka",
    "prejemnik_tip",
    "prejemnik_drzava_koda",
    "prejemnik_identificiran_za_ddv",
    "izdajatelj_naziv",
    "izdajatelj_naslov",
    "izdajatelj_davcna_stevilka",
    "izdajatelj_maticna_stevilka",
    "izdajatelj_drzava_koda",
    "izdajatelj_identificiran_za_ddv",
    "izdajatelj_trr",
    "oznaka_pogodbe",
    "objekt_naziv",
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
            napake[polje] = (
                "Zahtevana je končna Decimal vrednost z največ dvema decimalkama."
            )
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
        vrstica.lease_id,
        vrstica.lease_item_id,
        vrstica.opis,
        vrstica.osnova,
        vrstica.ddv_stopnja,
        vrstica.ddv_znesek,
        vrstica.skupaj,
        vrstica.ddv_nacin_obracuna,
        vrstica.ddv_klavzula,
        vrstica.ddv_rekap_skupina,
        vrstica.ddv_eslog_koda,
        vrstica.je_sorazmerno,
        vrstica.razlog_rocnega_popravka,
        vrstica.izvirno_izracunana_vrednost,
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
    podpisnik = izberi_veljavnega_podpisnika(original.company, datum_izdaje)
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
        izdajatelj_podpisnik_ime=podpisnik.ime_priimek,
        izdajatelj_podpisnik_funkcija=podpisnik.funkcija,
        izdajatelj_podpisnik_slika_pot=podpisnik.podpis_slika.name,
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
def _potrdi_korekcijski_osnutek(
    osnutek: Invoice,
    *,
    podpisnik_id: int,
    logo_vir: str,
    logo_pot: str,
    podpis_vir: str,
    podpis_pot: str,
) -> Invoice:
    invoice = preveri_uredljiv_osnutek(osnutek)
    if (
        invoice.document_type
        not in (
            Invoice.DocumentType.STORNO,
            Invoice.DocumentType.DOBROPIS,
        )
        or invoice.povezan_racun_id is None
    ):
        raise ValueError("Račun ni korekcijski osnutek.")
    original = _preveri_izvirnik_korekcije(invoice.povezan_racun)
    podpisnik = izberi_veljavnega_podpisnika(
        invoice.company, invoice.datum_izdaje
    )
    if podpisnik.pk != podpisnik_id or podpisnik.podpis_slika.name != podpis_vir:
        raise ValueError("Podpisnik se je med potrditvijo spremenil.")
    if invoice.company.logotip.name != logo_vir:
        raise ValueError("Logotip podjetja se je med potrditvijo spremenil.")
    invoice.eslog_konfiguracija_snapshot = _razresi_eslog_snapshot(invoice)
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
            "Vrstica dobropisa"
            if invoice.document_type == Invoice.DocumentType.DOBROPIS
            else "Izvirna vrstica storna",
        )

    _preveri_ddv_klavzule(vrstice)
    _preveri_mesanje_eslog_o(vrstice)

    invoice.osnova_skupaj = sum((v.osnova for v in vrstice), Decimal("0.00"))
    invoice.ddv_skupaj = sum((v.ddv_znesek for v in vrstice), Decimal("0.00"))
    invoice.skupaj_z_ddv = invoice.osnova_skupaj + invoice.ddv_skupaj
    tip = original.document_type
    invoice.izdajatelj_podpisnik_ime = podpisnik.ime_priimek
    invoice.izdajatelj_podpisnik_funkcija = podpisnik.funkcija
    invoice.izdajatelj_logotip_slika_pot = logo_pot
    invoice.izdajatelj_podpisnik_slika_pot = podpis_pot
    invoice.pdf_template_version = settings.PDF_TEMPLATE_VERSION
    zaporedna = dodeli_naslednjo_stevilko(invoice.company, tip, invoice.datum_izdaje)
    if tip == Invoice.DocumentType.NP:
        invoice.stevilka = f"NP-{zaporedna:05d}"
    else:
        invoice.stevilka = f"ST-{invoice.datum_izdaje.year}-{zaporedna:04d}"
    invoice.status = Invoice.Status.IZBRAN_ZA_POSILJANJE
    invoice.save(
        update_fields=[
            "osnova_skupaj",
            "ddv_skupaj",
            "skupaj_z_ddv",
            "stevilka",
            "status",
            "izdajatelj_podpisnik_ime",
            "izdajatelj_podpisnik_funkcija",
            "izdajatelj_logotip_slika_pot",
            "izdajatelj_podpisnik_slika_pot",
            "pdf_template_version",
            "eslog_konfiguracija_snapshot",
        ]
    )
    return invoice


def potrdi_korekcijski_osnutek(osnutek: Invoice) -> Invoice:
    invoice = preveri_uredljiv_osnutek(osnutek)
    podpisnik = izberi_veljavnega_podpisnika(
        invoice.company, invoice.datum_izdaje
    )
    logo_vir = invoice.company.logotip.name
    podpis_vir = podpisnik.podpis_slika.name
    logo_pot = shrani_nespremenljiv_asset(logo_vir)
    podpis_pot = shrani_nespremenljiv_asset(podpis_vir)
    return _potrdi_korekcijski_osnutek(
        osnutek,
        podpisnik_id=podpisnik.pk,
        logo_vir=logo_vir,
        logo_pot=logo_pot,
        podpis_vir=podpis_vir,
        podpis_pot=podpis_pot,
    )


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
    neznano = DeltaRazlaga(
        lease_item_id, "VZROK_NEZNAN", "vzrok neznan – preveri ročno"
    )
    if lease_item_id is None:
        return neznano
    postavka = LeaseItem.objects.filter(
        pk=lease_item_id, lease_id=osnutek.lease_id
    ).first()
    if postavka is None:
        return neznano

    if not stare and nove:
        zacetek = postavka.datum_zacetka_postavke
        if zacetek and prejsnji.obdobje_do < zacetek <= osnutek.obdobje_do:
            return DeltaRazlaga(
                lease_item_id,
                "NOVA_POSTAVKA",
                f"Nova postavka na pogodbi (dodana {zacetek})",
            )
    if stare and not nove:
        konec = postavka.datum_prenehanja_postavke
        if konec and prejsnji.obdobje_od <= konec < osnutek.obdobje_od:
            return DeltaRazlaga(
                lease_item_id,
                "PRENEHANJE_POSTAVKE",
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
                lease_item_id,
                "INDEKSACIJA",
                f"Indeksacija postavke z dne {dogodki[-1].datum_uskladitve}",
            )
        if stara.osnova == nova.osnova and not isti_ddv:
            return DeltaRazlaga(
                lease_item_id,
                "SPREMEMBA_DDV",
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
                    lease_item_id,
                    "ROCNI_POPRAVEK",
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
        (DeltaRazlaga(None, "PREJSNJI_MESEC_NI_IZDAN", "Prejšnji mesec ni bil izdan"),)
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
            prejsnji.pk,
            False,
            None,
            (DeltaRazlaga(None, "BREZ_RAZLIKE", "Znesek je nespremenjen"),) + opozorila,
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
        prejsnji.pk,
        True,
        all(razlaga.koda != "VZROK_NEZNAN" for razlaga in razlage),
        tuple(razlage) + opozorila,
    )


def _izracunaj_vrstice_np(
    lease: Lease, obdobje_od: date, obdobje_do: date, *, validate_eslog: bool = True
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
                ddv_eslog_koda=(
                    _preveri_eslog_kodo(
                        izracun.ddv_nacin_obracuna,
                        postavka.vat_regime.eslog_koda,
                        f"DDV režim {postavka.vat_regime.sifra}",
                    )
                    if validate_eslog
                    else ""
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
            vrstice,
            skupaj,
            False,
            tuple(DeltaRazlaga(None, b.koda, b.sporocilo) for b in blokade),
            blokade[0].sporocilo,
        )

    izjeme = [
        DeltaRazlaga(v.lease_item_id, "SORAZMERNO", "Sorazmerni obračun postavke")
        for v in vrstice
        if v.je_sorazmerno
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


CALCULATION_SNAPSHOT_VERSION = 3


def _decimal_string(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


def _previous_invoice_snapshot(invoice_id: int | None) -> dict | None:
    if invoice_id is None:
        return None
    invoice = Invoice.objects.get(pk=invoice_id)
    total = invoice.skupaj_z_ddv
    if total is None:
        total = sum(
            InvoiceLine.objects.filter(invoice=invoice).values_list("skupaj", flat=True),
            Decimal("0.00"),
        )
    lines = InvoiceLine.objects.filter(invoice=invoice).order_by(
        "lease_item_id", "pk"
    )
    is_migration_baseline = (
        invoice.status == Invoice.Status.ARHIVIRAN_NI_IZDAN
        and invoice.razlog_arhiviranja == MIGRACIJSKI_RAZLOG
    )
    return {
        "id": invoice.pk,
        "is_migration_baseline": is_migration_baseline,
        "number": invoice.stevilka,
        "period_start": invoice.obdobje_od.isoformat(),
        "period_end": invoice.obdobje_do.isoformat(),
        "total": _decimal_string(total),
        "issuer": {
            "name": invoice.izdajatelj_naziv,
            "address": invoice.izdajatelj_naslov,
            "country": invoice.izdajatelj_drzava_koda,
            "vat_registered": invoice.izdajatelj_identificiran_za_ddv,
            "tax_number": invoice.izdajatelj_davcna_stevilka,
            "registration_number": invoice.izdajatelj_maticna_stevilka,
            "iban": invoice.izdajatelj_trr,
        },
        "recipient": {
            "type": invoice.prejemnik_tip,
            "name": invoice.prejemnik_naziv,
            "address": invoice.prejemnik_naslov,
            "country": invoice.prejemnik_drzava_koda,
            "vat_registered": invoice.prejemnik_identificiran_za_ddv,
            "tax_number": invoice.prejemnik_davcna_stevilka,
            "vat_id": invoice.prejemnik_ddv_identifikacijska_stevilka,
            "registration_number": invoice.prejemnik_maticna_stevilka,
        },
        "contract_reference": invoice.oznaka_pogodbe,
        "property_label": invoice.objekt_naziv,
        "delivery_contacts": (
            {} if is_migration_baseline else invoice.dostavni_kontakti_snapshot
        ),
        "delivery_contacts_available": (
            not is_migration_baseline and bool(invoice.dostavni_kontakti_snapshot)
        ),
        "lines": [
            {
                "lease_item_id": line.lease_item_id,
                "vat_method": line.ddv_nacin_obracuna,
                "vat_rate": _decimal_string(line.ddv_stopnja),
                "vat_clause": line.ddv_klavzula,
                "vat_summary_group": line.ddv_rekap_skupina,
            }
            for line in lines
        ],
    }


def _exception(code, message, lease_item_id=None):
    return {"code": code, "message": message, "lease_item_id": lease_item_id}


def _delivery_snapshot(company, tenant, contacts) -> dict:
    result = {
        "tenant_email": tenant.email,
        "accountant_bcc": company.email_racunovodja,
        "to": [],
        "cc": [],
        "bcc": [],
    }
    for contact in contacts:
        result[contact.vloga.lower()].append(
            {"email": contact.email, "channel": contact.kanal}
        )
    return result


def _comparison_exceptions(current, previous):
    if previous is None or previous["is_migration_baseline"]:
        return []
    exceptions = []
    fields = {
        "name": ("NAZIV", "Spremenjen naziv izdajatelja."),
        "address": ("NASLOV", "Spremenjen naslov izdajatelja."),
        "country": ("DRZAVA", "Spremenjena država izdajatelja."),
        "vat_registered": ("DDV_STATUS", "Spremenjen DDV status izdajatelja."),
        "tax_number": ("DAVCNA_STEVILKA", "Spremenjena davčna številka izdajatelja."),
        "registration_number": (
            "MATICNA_STEVILKA", "Spremenjena matična številka izdajatelja."
        ),
        "iban": ("IBAN", "Spremenjen IBAN izdajatelja."),
    }
    for field, (code, message) in fields.items():
        if current["issuer"].get(field) != previous["issuer"].get(field):
            exceptions.append(_exception(
                f"SPREMEMBA_IZDAJATELJA_{code}",
                message,
            ))
    fields = {
        "type": ("TIP", "Spremenjen tip prejemnika."),
        "name": ("NAZIV", "Spremenjen naziv prejemnika."),
        "address": ("NASLOV", "Spremenjen naslov prejemnika."),
        "country": ("DRZAVA", "Spremenjena država prejemnika."),
        "vat_registered": ("DDV_STATUS", "Spremenjen DDV status prejemnika."),
        "tax_number": ("DAVCNA_STEVILKA", "Spremenjena davčna številka prejemnika."),
        "vat_id": (
            "DDV_IDENTIFIKACIJA", "Spremenjena DDV identifikacijska številka prejemnika."
        ),
        "registration_number": (
            "MATICNA_STEVILKA", "Spremenjena matična številka prejemnika."
        ),
    }
    for field, (code, message) in fields.items():
        if current["recipient"].get(field) != previous["recipient"].get(field):
            exceptions.append(_exception(
                f"SPREMEMBA_PREJEMNIKA_{code}",
                message,
            ))
    if current["contract_reference"] != previous["contract_reference"]:
        exceptions.append(_exception(
            "SPREMEMBA_SKLICA_POGODBE", "Spremenjen sklic na pogodbo."
        ))
    if current["property_label"] != previous["property_label"]:
        exceptions.append(_exception(
            "SPREMEMBA_OZNAKE_OBJEKTA", "Spremenjena oznaka objekta."
        ))
    return exceptions


def _vat_exceptions(current_lines, previous):
    if previous is None:
        return []
    old = {}
    for line in previous["lines"]:
        old.setdefault(line["lease_item_id"], []).append(
            tuple(line.get(field) for field in (
                "vat_method", "vat_rate", "vat_clause", "vat_summary_group"
            ))
        )
    new = {}
    for line in current_lines:
        new.setdefault(line["lease_item_id"], []).append(
            tuple(line.get(field) for field in (
                "vat_method", "vat_rate", "vat_clause", "vat_summary_group"
            ))
        )
    return [
        _exception(
            "SPREMEMBA_DDV",
            "Spremenjen DDV režim postavke.",
            lease_item_id,
        )
        for lease_item_id in sorted(old.keys() & new.keys(), key=lambda value: value or 0)
        if sorted(old[lease_item_id]) != sorted(new[lease_item_id])
    ]


def _lifecycle_exceptions(lease, period_start, period_end):
    exceptions = []
    end = lease.datum_konca
    fixed = lease.tip_trajanja == Lease.TipTrajanja.DOLOCEN_CAS and end is not None
    if fixed and period_start <= end <= period_end:
        exceptions.append(_exception(
            "POGODBA_POTECE_V_OBDOBJU",
            f"Pogodba poteče v obračunskem obdobju ({end:%d.%m.%Y}).",
        ))
    elif fixed and period_end < end <= period_end + timedelta(days=30):
        exceptions.append(_exception(
            "POGODBA_POTECE_V_30_DNEH",
            f"Pogodba poteče v naslednjih 30 dneh ({end:%d.%m.%Y}).",
        ))
    if fixed and lease.samodejno_podaljsanje and period_start <= end <= period_end + timedelta(days=30):
        suffix = (
            f" za {lease.obdobje_podaljsanja_mesecev} mesecev"
            if lease.obdobje_podaljsanja_mesecev else ""
        )
        exceptions.append(_exception(
            "SAMODEJNO_PODALJSANJE_OB_KONCU",
            f"Pogodba s samodejnim podaljšanjem{suffix} doseže konec {end:%d.%m.%Y}.",
        ))
    termination = lease.datum_prenehanja
    if lease.status == Lease.Status.ODPOVEDANA and termination:
        termination_month = termination.replace(day=1)
        previous_month = (termination_month - timedelta(days=1)).replace(day=1)
        if period_start == termination_month:
            exceptions.append(_exception(
                "ODPOVED_ZADNJI_MESEC", "Pogodba je v zadnjem mesecu zaračunavanja."
            ))
        elif period_start == previous_month:
            exceptions.append(_exception(
                "ODPOVED_PREDZADNJI_MESEC",
                "Pogodba je v predzadnjem mesecu zaračunavanja.",
            ))
    return exceptions


def _candidate_fingerprint(candidate, *, company, period_start, period_end, issue_date):
    value = {
        "schema_version": CALCULATION_SNAPSHOT_VERSION,
        "period_start": period_start,
        "period_end": period_end,
        "issue_date": issue_date,
        "company": company,
        "candidate": candidate,
    }
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _previous_month_archived_snapshot(lease, period_start: date) -> dict:
    previous_end = period_start - timedelta(days=1)
    previous_start = previous_end.replace(day=1)
    invoice = (
        Invoice.objects.filter(
            company=lease.unit.property.company,
            lease=lease,
            document_type=Invoice.DocumentType.NP,
            status=Invoice.Status.ARHIVIRAN_NI_IZDAN,
            obdobje_od=previous_start,
            obdobje_do=previous_end,
        )
        .exclude(razlog_arhiviranja=MIGRACIJSKI_RAZLOG)
        .order_by("-datum_izdaje", "-pk")
        .first()
    )
    return {
        "exists": invoice is not None,
        "invoice_id": invoice.pk if invoice else None,
        "period_start": invoice.obdobje_od.isoformat() if invoice else None,
        "period_end": invoice.obdobje_do.isoformat() if invoice else None,
        "archive_reason": invoice.razlog_arhiviranja if invoice else None,
    }


def _monthly_candidate(lease, period_start, period_end, issue_date):
    lines = _izracunaj_vrstice_np(
        lease, period_start, period_end, validate_eslog=False
    )
    total = sum((line.skupaj for line in lines), Decimal("0.00"))
    draft = Invoice(
        company=lease.unit.property.company,
        lease=lease,
        document_type=Invoice.DocumentType.NP,
        status=Invoice.Status.OSNUTEK,
        datum_izdaje=issue_date,
        obdobje_od=period_start,
        obdobje_do=period_end,
    )
    delta = _primerjaj_delta_np(draft, lines)
    if not lines:
        start, end = efektivna_veljavnost_pogodbe(lease)
        reason = (
            "Pogodba ne velja v tem obdobju"
            if not dnevi_v_obdobju(period_start, period_end, start, end)
            else "Pogodba nima obračunljivih postavk v tem obdobju"
        )
        exceptions = tuple(
            item for item in delta.razlage if item.koda != "BREZ_RAZLIKE"
        )
        candidate = RacunKandidat((), total, False, exceptions, reason)
    else:
        exceptions = [
            DeltaRazlaga(line.lease_item_id, "SORAZMERNO", "Sorazmerni obračun postavke")
            for line in lines
            if line.je_sorazmerno
        ]
        exceptions.extend(item for item in delta.razlage if item.koda != "BREZ_RAZLIKE")
        candidate = RacunKandidat(lines, total, True, tuple(exceptions))
    return candidate, delta


def _calculation_snapshot(batch, cycle, issues):
    company = batch.company
    period_end = cycle.obdobje.replace(
        day=monthrange(cycle.obdobje.year, cycle.obdobje.month)[1]
    )
    blockers = sorted(
        (issue.copy() for issue in issues if issue["severity"] == "BLOCKER"),
        key=lambda item: (item["code"], item["model"], item["object_id"], item.get("field") or ""),
    )
    warnings = sorted(
        (issue.copy() for issue in issues if issue["severity"] == "WARNING"),
        key=lambda item: (item["code"], item["model"], item["object_id"], item.get("field") or ""),
    )
    base = {
        "schema_version": CALCULATION_SNAPSHOT_VERSION,
        "company_id": company.pk,
        "period_start": cycle.obdobje.isoformat(),
        "period_end": period_end.isoformat(),
        "issue_date": cycle.datum_izdaje.isoformat(),
        "preflight_warnings": warnings,
        "blockers": blockers,
        "candidates": [],
    }
    if blockers:
        return base

    signatory = izberi_veljavnega_podpisnika(company, cycle.datum_izdaje)
    sample_confirmation = veljavna_potrditev_vzorca(company).potrditev
    pdf_template_version = sample_confirmation.pdf_template_version
    base["pdf_template_version"] = pdf_template_version
    base["company"] = {
        "id": company.pk,
        "name": company.naziv,
        "address": company.naslov,
        "country": company.drzava_koda,
        "tax_number": company.davcna_stevilka,
        "registration_number": company.maticna_stevilka,
        "vat_registered": company.ddv_zavezanec,
        "iban": company.trr,
        "default_payment_due_days": company.rok_placila_dni_privzeto,
        "logo_source": company.logotip.name,
        "logo_checksum": company.logotip_checksum or None,
        "signatory": {
            "id": signatory.pk,
            "name": signatory.ime_priimek,
            "function": signatory.funkcija,
            "signature_source": signatory.podpis_slika.name,
            "signature_checksum": signatory.podpis_checksum or None,
        },
    }
    leases = Lease.objects.filter(
        unit__property__company=company,
        arhivirana=False,
        unit__property__aktivna=True,
        unit__aktivna=True,
        tenant__aktiven=True,
    ).select_related("tenant", "unit__property__company").order_by("pk")
    for lease in leases:
        start, end = efektivna_veljavnost_pogodbe(lease)
        if not dnevi_v_obdobju(cycle.obdobje, period_end, start, end):
            continue
        candidate, delta = _monthly_candidate(
            lease, cycle.obdobje, period_end, cycle.datum_izdaje
        )
        tenant = lease.tenant
        contacts = list(RecipientContact.objects.filter(
            lease=lease, aktiven=True
        ).order_by("vloga", "email", "kanal", "pk"))
        delivery = _delivery_snapshot(company, tenant, contacts)
        previous = _previous_invoice_snapshot(delta.prejsnji_racun_id)
        current_header = {
            "issuer": {
                "name": company.naziv,
                "address": company.naslov,
                "country": company.drzava_koda,
                "vat_registered": company.ddv_zavezanec,
                "tax_number": company.davcna_stevilka,
                "registration_number": company.maticna_stevilka,
                "iban": company.trr,
            },
            "recipient": {
                "type": tenant.tip,
                "name": tenant.naziv,
                "address": tenant.naslov,
                "country": tenant.drzava_koda,
                "vat_registered": tenant.identificiran_za_ddv,
                "tax_number": tenant.davcna_stevilka,
                "vat_id": tenant.ddv_identifikacijska_stevilka,
                "registration_number": tenant.maticna_stevilka,
            },
            "contract_reference": lease.oznaka_pogodbe,
            "property_label": lease.unit.property.naziv,
        }
        exceptions = sorted(
            ({"code": item.koda, "message": item.opis, "lease_item_id": item.lease_item_id}
             for item in candidate.izjeme if item.koda != "SPREMEMBA_DDV"),
            key=lambda item: (item["code"], item["lease_item_id"] or 0, item["message"]),
        )
        lines = [{
            "lease_item_id": line.lease_item_id, "description": line.opis,
            "net": _decimal_string(line.osnova),
            "vat_rate": _decimal_string(line.ddv_stopnja),
            "vat": _decimal_string(line.ddv_znesek),
            "total": _decimal_string(line.skupaj),
            "vat_method": line.ddv_nacin_obracuna,
            "vat_clause": line.ddv_klavzula,
            "vat_summary_group": line.ddv_rekap_skupina,
            "proportional": line.je_sorazmerno,
        } for line in sorted(candidate.vrstice, key=lambda line: (line.lease_item_id or 0, line.opis))]
        if delta.obstaja_razlika:
            exceptions.append(_exception(
                "ZNESEK_SPREMENJEN",
                (
                    "Znesek se razlikuje od uvoženega migracijskega izhodišča."
                    if previous and previous["is_migration_baseline"]
                    else "Znesek se razlikuje od zadnjega dejansko izdanega računa."
                ),
            ))
        exceptions.extend(_comparison_exceptions(current_header, previous))
        exceptions.extend(_vat_exceptions(lines, previous))
        exceptions.extend(_lifecycle_exceptions(lease, cycle.obdobje, period_end))
        archived = _previous_month_archived_snapshot(lease, cycle.obdobje)
        if archived["exists"]:
            exceptions.append(_exception(
                "PREJSNJI_MESEC_ARHIVIRAN_NI_IZDAN",
                "Prejšnji mesec je bil račun arhiviran kot neizdan.",
            ))
        if (
            previous
            and previous["delivery_contacts_available"]
            and delivery != previous["delivery_contacts"]
        ):
            exceptions.append(_exception(
                "SPREMEMBA_DOSTAVNIH_KONTAKTOV",
                "Spremenjen e-naslov ali dostavni kontakti.",
            ))
        exceptions = sorted(
            {(
                item["code"], item["lease_item_id"], item["message"]
            ): item for item in exceptions}.values(),
            key=lambda item: (item["code"], item["lease_item_id"] or 0, item["message"]),
        )
        candidate_snapshot = {
            "lease_id": lease.pk,
            "contract_reference": lease.oznaka_pogodbe,
            "property_id": lease.unit.property_id,
            "property_label": lease.unit.property.naziv,
            "unit_id": lease.unit_id,
            "unit_label": lease.unit.oznaka,
            "tenant": {
                "id": tenant.pk, "type": tenant.tip, "name": tenant.naziv,
                "address": tenant.naslov, "country": tenant.drzava_koda,
                "tax_number": tenant.davcna_stevilka,
                "vat_id": tenant.ddv_identifikacijska_stevilka,
                "registration_number": tenant.maticna_stevilka,
                "vat_registered": tenant.identificiran_za_ddv,
                "email": tenant.email,
            },
            "recipient_contacts": [
                {
                    "id": contact.pk,
                    "email": contact.email,
                    "role": contact.vloga,
                    "channel": contact.kanal,
                }
                for contact in contacts
            ],
            "delivery_contacts": delivery,
            "historical_delivery_contacts_available": bool(
                previous and previous["delivery_contacts_available"]
            ),
            "pdf_template_version": pdf_template_version,
            "payment_due_days": lease.rok_placila_dni,
            "due_date": (cycle.datum_izdaje + timedelta(days=lease.rok_placila_dni)).isoformat(),
            "will_issue": candidate.se_izda,
            "non_issue_reason": candidate.razlog_neizdaje,
            "lines": lines,
            "totals": {
                "net": _decimal_string(sum((line.osnova for line in candidate.vrstice), Decimal("0.00"))),
                "vat": _decimal_string(sum((line.ddv_znesek for line in candidate.vrstice), Decimal("0.00"))),
                "total": _decimal_string(candidate.skupaj_z_ddv),
            },
            "exceptions": exceptions,
            "previous_invoice": previous,
            "first_issue": delta.prejsnji_racun_id is None,
            "previous_month_archived_not_issued": (
                archived
            ),
            "lease_state": {
                "type": lease.tip_trajanja, "status": lease.status,
                "start_date": lease.datum_zacetka.isoformat(),
                "end_date": lease.datum_konca.isoformat() if lease.datum_konca else None,
                "cancellation_date": lease.datum_odpovedi.isoformat() if lease.datum_odpovedi else None,
                "termination_date": lease.datum_prenehanja.isoformat() if lease.datum_prenehanja else None,
                "automatic_extension": lease.samodejno_podaljsanje,
                "automatic_extension_months": lease.obdobje_podaljsanja_mesecev,
            },
        }
        candidate_snapshot["candidate_fingerprint"] = _candidate_fingerprint(
            candidate_snapshot,
            company=base["company"],
            period_start=base["period_start"],
            period_end=base["period_end"],
            issue_date=base["issue_date"],
        )
        base["candidates"].append(candidate_snapshot)
    return base


def aktivni_izracuni_mesecnega_cikla(cycle: MonthlyCycle):
    return cycle.batches.filter(
        preflight_status=MonthlyBatch.PreflightStatus.PASSED,
        calculation_status=MonthlyBatch.CalculationStatus.CALCULATED,
        calculation_schema_version=CALCULATION_SNAPSHOT_VERSION,
    )


def _aktivni_kandidati(cycle: MonthlyCycle):
    for batch in aktivni_izracuni_mesecnega_cikla(cycle).order_by("company_id", "pk"):
        for candidate in batch.calculation_snapshot.get("candidates", []):
            lease_id = candidate.get("lease_id")
            fingerprint = candidate.get("candidate_fingerprint")
            if not lease_id or not fingerprint:
                raise ValidationError(
                    "Aktivni kandidat mora vsebovati lease_id in candidate_fingerprint."
                )
            yield batch, candidate, lease_id, fingerprint


def _zahtevaj_cetrti_korak(cycle: MonthlyCycle) -> MonthlyCycle:
    cycle = MonthlyCycle.objects.get(pk=cycle.pk)
    if cycle.trenutni_korak != MonthlyCycle.Korak.PAKETNI_PREGLED:
        raise NeveljavenPrehodMesecnegaCikla(
            "Pregled kandidatov je dovoljen samo v koraku 4."
        )
    return cycle


def uskladi_preglede_mesecnega_cikla(cycle: MonthlyCycle) -> list[MonthlyCandidateReview]:
    cycle = _zahtevaj_cetrti_korak(cycle)
    desired = [
        MonthlyCandidateReview(
            batch=batch,
            lease_id=lease_id,
            candidate_fingerprint=fingerprint,
            selected=bool(candidate["will_issue"]),
            requires_confirmation=(
                bool(candidate.get("exceptions")) and bool(candidate["will_issue"])
            ),
        )
        for batch, candidate, lease_id, fingerprint in _aktivni_kandidati(cycle)
    ]
    with transaction.atomic():
        MonthlyCandidateReview.objects.bulk_create(desired, ignore_conflicts=True)
    return trenutni_pregledi_mesecnega_cikla(cycle)


def trenutni_pregledi_mesecnega_cikla(cycle: MonthlyCycle) -> list[MonthlyCandidateReview]:
    identities = {
        (batch.pk, lease_id, fingerprint)
        for batch, _candidate, lease_id, fingerprint in _aktivni_kandidati(cycle)
    }
    if not identities:
        return []
    batch_ids = {identity[0] for identity in identities}
    reviews = MonthlyCandidateReview.objects.filter(batch_id__in=batch_ids).select_related(
        "batch", "lease"
    )
    return [
        review
        for review in reviews
        if (review.batch_id, review.lease_id, review.candidate_fingerprint) in identities
    ]


def _preveri_izdajni_snapshot(candidate: dict) -> None:
    lines = candidate.get("lines")
    totals = candidate.get("totals")
    if not isinstance(lines, list) or not lines or not isinstance(totals, dict):
        raise ValidationError("Posnetek izbranega kandidata je nepopoln.")
    try:
        net = Decimal(str(totals["net"]))
        vat = Decimal(str(totals["vat"]))
        total = Decimal(str(totals["total"]))
        line_net = sum((Decimal(str(line["net"])) for line in lines), Decimal("0.00"))
        line_vat = sum((Decimal(str(line["vat"])) for line in lines), Decimal("0.00"))
        line_total = sum(
            (Decimal(str(line["total"])) for line in lines), Decimal("0.00")
        )
    except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
        raise ValidationError("Posnetek izbranega kandidata ni veljaven.") from exc
    if line_net != net or line_vat != vat or line_total != total or net + vat != total:
        raise ValidationError("Vsote v posnetku izbranega kandidata niso skladne.")


def zamrzni_paket_za_potrditev(cycle: MonthlyCycle) -> MonthlyCycle:
    with transaction.atomic():
        cycle = MonthlyCycle.objects.get(pk=cycle.pk)
        if cycle.trenutni_korak != MonthlyCycle.Korak.PAKETNI_PREGLED:
            raise NeveljavenPrehodMesecnegaCikla(
                "Paket je mogoče zamrzniti samo v koraku 4."
            )
        if not so_vsi_uspesni_izracuni_aktivni(cycle):
            raise ValidationError(
                "Paket nima veljavnega aktivnega izračuna. "
                "Vrnite se na pregled in osvežite izračun."
            )
        for batch, candidate, lease_id, fingerprint in _aktivni_kandidati(cycle):
            reviews = MonthlyCandidateReview.objects.filter(
                batch=batch, lease_id=lease_id
            )
            try:
                review = reviews.get(candidate_fingerprint=fingerprint)
            except MonthlyCandidateReview.DoesNotExist as exc:
                message = (
                    "Prstni odtis pregleda se ne ujema s trenutnim kandidatom."
                    if reviews.exists()
                    else "Manjka trenutni pregled kandidata."
                )
                raise ValidationError(message) from exc
            will_issue = candidate.get("will_issue") is True
            if review.selected:
                if not will_issue:
                    raise ValidationError(
                        "Izbrani pregled je v neskladju s sistemsko neizdajnim kandidatom."
                    )
                if review.non_issue_reason.strip():
                    raise ValidationError("Izbrani kandidat ne sme imeti razloga neizdaje.")
                if review.requires_confirmation and review.confirmed_at is None:
                    raise ValidationError("Izjema izbranega kandidata ni potrjena.")
                _preveri_izdajni_snapshot(candidate)
            elif will_issue and not review.non_issue_reason.strip():
                raise ValidationError("Manjka razlog neizdaje kandidata.")
            elif not will_issue and not str(
                candidate.get("non_issue_reason") or ""
            ).strip():
                raise ValidationError("Sistemsko neizdan kandidat nima razloga.")
        cycle.trenutni_korak = MonthlyCycle.Korak.POTRDITEV_PAKETA
        cycle.save(update_fields=["trenutni_korak", "updated_at"])
        return cycle


def trenutni_mesecni_kandidat(cycle, *, lease_id, candidate_fingerprint):
    cycle = _zahtevaj_cetrti_korak(cycle)
    for batch, candidate, current_lease_id, fingerprint in _aktivni_kandidati(cycle):
        if current_lease_id == lease_id and fingerprint == candidate_fingerprint:
            return batch, candidate
    raise ValidationError("Kandidat ni trenutni aktivni kandidat.")


def _trenutni_pregled_in_kandidat(cycle, lease_id, candidate_fingerprint):
    batch, candidate = trenutni_mesecni_kandidat(
        cycle,
        lease_id=lease_id,
        candidate_fingerprint=candidate_fingerprint,
    )
    return (
        MonthlyCandidateReview.objects.get(
            batch=batch,
            lease_id=lease_id,
            candidate_fingerprint=candidate_fingerprint,
        ),
        candidate,
    )


def nastavi_izbiro_kandidata(
    cycle: MonthlyCycle,
    *,
    lease_id: int,
    candidate_fingerprint: str,
    selected: bool,
    non_issue_reason: str = "",
) -> MonthlyCandidateReview:
    review, candidate = _trenutni_pregled_in_kandidat(
        cycle, lease_id, candidate_fingerprint
    )
    reason = non_issue_reason.strip()
    if selected and not candidate["will_issue"]:
        raise ValidationError("Kandidata, ki ga sistem ne izda, ni dovoljeno izbrati.")
    if not selected and candidate["will_issue"] and not reason:
        raise ValidationError("Za neizdajo kandidata je potreben razlog.")
    review.selected = selected
    review.non_issue_reason = "" if selected else reason
    fields = ["selected", "non_issue_reason", "updated_at"]
    if not selected:
        review.confirmed_at = None
        review.reviewed_at = timezone.now()
        fields.extend(["confirmed_at", "reviewed_at"])
    review.save(update_fields=fields)
    return review


def potrdi_izjemo_kandidata(
    cycle: MonthlyCycle, *, lease_id: int, candidate_fingerprint: str
) -> MonthlyCandidateReview:
    review, candidate = _trenutni_pregled_in_kandidat(
        cycle, lease_id, candidate_fingerprint
    )
    if not review.selected:
        raise ValidationError("Neizbranega kandidata ni dovoljeno potrditi.")
    if not review.requires_confirmation or not (
        bool(candidate.get("exceptions")) and candidate["will_issue"]
    ):
        raise ValidationError("Kandidat ne zahteva potrditve izjeme.")
    now = timezone.now()
    review.confirmed_at = now
    review.reviewed_at = now
    review.save(update_fields=["confirmed_at", "reviewed_at", "updated_at"])
    return review


def so_vsi_uspesni_izracuni_aktivni(cycle: MonthlyCycle) -> bool:
    active = aktivni_izracuni_mesecnega_cikla(cycle)
    return active.exists() and not cycle.batches.filter(
        preflight_status=MonthlyBatch.PreflightStatus.PASSED
    ).exclude(pk__in=active.values("pk")).exists()


def izracunaj_mesecne_kandidate(
    cycle: MonthlyCycle, *, samo_brez_aktivnega_izracuna: bool = False
) -> list[MonthlyBatch]:
    cycle = MonthlyCycle.objects.get(pk=cycle.pk)
    if cycle.trenutni_korak == MonthlyCycle.Korak.PAKETNI_PREGLED:
        if not samo_brez_aktivnega_izracuna:
            raise NeveljavenPrehodMesecnegaCikla(
                "V koraku 4 je dovoljen samo izračun novih upravičenih podjetij."
            )
    elif cycle.trenutni_korak != MonthlyCycle.Korak.IZRACUN_OSNUTKOV:
        raise NeveljavenPrehodMesecnegaCikla(
            "Izračun je dovoljen samo v koraku 3 ali 4."
        )
    period_end = cycle.obdobje.replace(
        day=monthrange(cycle.obdobje.year, cycle.obdobje.month)[1]
    )
    results = []
    batches = MonthlyBatch.objects.filter(
        cycle=cycle, preflight_status=MonthlyBatch.PreflightStatus.PASSED
    )
    if samo_brez_aktivnega_izracuna:
        batches = batches.exclude(
            calculation_status=MonthlyBatch.CalculationStatus.CALCULATED,
            calculation_schema_version=CALCULATION_SNAPSHOT_VERSION,
        )
    batches = batches.select_related("company").order_by("company_id")
    for batch in batches:
        issues = _company_preflight_issues(batch.company, cycle, period_end)
        snapshot = _calculation_snapshot(batch, cycle, issues)
        status = (
            MonthlyBatch.CalculationStatus.BLOCKED
            if snapshot["blockers"]
            else MonthlyBatch.CalculationStatus.CALCULATED
        )
        with transaction.atomic():
            MonthlyBatch.objects.filter(pk=batch.pk).update(
                calculation_status=status,
                calculated_at=timezone.now(),
                calculation_schema_version=CALCULATION_SNAPSHOT_VERSION,
                calculation_snapshot=snapshot,
            )
        batch.refresh_from_db()
        results.append(batch)
    return results
