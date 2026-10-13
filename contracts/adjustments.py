"""Late indexation evidence only. No invoice issuance or adjustment consumption."""

import hashlib
import json
from calendar import monthrange
from copy import deepcopy
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone

from catalogs.models import ESLOG_KODE_DDV, VatRegime
from core.models import AuditLogEntry
from invoicing.models import Invoice, InvoiceLine

from .models import IndexationEvent, PoracunIndeksacije
from .vat_eligibility import preveri_postavko_ddv, preveri_upravicenost_prejemnika


def _json(value):
    return json.loads(json.dumps(value, cls=DjangoJSONEncoder, sort_keys=True))


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _reason(code, message):
    return {"code": code, "message": message}


def _period(line, event, verified):
    invoice = line.invoice
    start, end = invoice.obdobje_od, invoice.obdobje_do
    month_days = monthrange(start.year, start.month)[1]
    full_month = start.day == 1 and end == date(start.year, start.month, month_days)
    if end < start or (start.year, start.month) != (end.year, end.month):
        return None  # No allocation of one historical amount across months.
    if line.je_sorazmerno or not full_month:
        evidence = verified.get(line.pk, {})
        start, end = evidence.get("start"), evidence.get("end")
        reference = evidence.get("reference")
        if (
            type(start) is not date
            or type(end) is not date
            or not isinstance(reference, str)
            or not reference.strip()
            or not invoice.obdobje_od <= start <= end <= invoice.obdobje_do
        ):
            return None
    else:
        reference = "Nesorazmerna izdana vrstica in mesečno obdobje izvirnika"
    if start < event.datum_uskladitve:
        return None  # No allocation of a historical line across effective dates.
    return {
        "start": start,
        "end": end,
        "reference": reference,
        "month_days": month_days,
    }


def _corrections(line):
    invoice = line.invoice
    # Include unlinked corrections for the same lease/period and linked documents
    # regardless of their dates or company: conflicting links need reconciliation.
    documents = Invoice.objects.filter(
        Q(povezan_racun_id=invoice.pk)
        | Q(pk=invoice.povezan_racun_id)
        | (
            Q(document_type__in=["STORNO", "DOBROPIS"])
            & (
                Q(lease_id=line.lease_item.lease_id)
                | Q(invoiceline__lease_item_id=line.lease_item_id)
            )
            & Q(obdobje_od__lte=invoice.obdobje_do, obdobje_do__gte=invoice.obdobje_od)
        )
    ).distinct()
    ids = set(documents.values_list("pk", flat=True))
    frontier = ids
    while frontier:
        children = (
            set(
                Invoice.objects.filter(povezan_racun_id__in=frontier).values_list(
                    "pk", flat=True
                )
            )
            - ids
            - {invoice.pk}
        )
        ids.update(children)
        frontier = children
    return [
        {
            "id": doc.pk,
            "company_id": doc.company_id,
            "lease_id": doc.lease_id,
            "type": doc.document_type,
            "number": doc.stevilka,
            "status": doc.status,
            "original_id": doc.povezan_racun_id,
            "reason": doc.razlog,
            "lines": list(doc.invoiceline_set.order_by("pk").values()),
        }
        for doc in Invoice.objects.filter(pk__in=ids).order_by("pk")
    ]


def _vat_errors(line, period):
    invoice, item = line.invoice, line.lease_item
    tenant, company, regime = (
        item.lease.tenant,
        item.lease.unit.property.company,
        item.vat_regime,
    )
    try:
        preveri_postavko_ddv(item)
        preveri_upravicenost_prejemnika(
            sifra=line.ddv_rezim_sifra,
            zahteva_polni_odbitek=line.ddv_zahteva_polni_odbitek,
            tip=invoice.prejemnik_tip,
            identificiran_za_ddv=invoice.prejemnik_identificiran_za_ddv,
            polni_odbitek=invoice.prejemnik_polni_odbitek,
            najemnik=invoice.prejemnik_naziv,
            postavka=f"#{item.pk}",
        )
        recipient = {
            "tip": tenant.tip,
            "naziv": tenant.naziv,
            "naslov": tenant.naslov,
            "drzava_koda": tenant.drzava_koda,
            "davcna_stevilka": tenant.davcna_stevilka,
            "ddv_identifikacijska_stevilka": tenant.ddv_identifikacijska_stevilka,
            "maticna_stevilka": tenant.maticna_stevilka,
            "identificiran_za_ddv": tenant.identificiran_za_ddv,
            "polni_odbitek": tenant.ddv_zavezanec_s_polnim_odbitkom,
        }
        if any(
            getattr(invoice, "prejemnik_" + key) != value
            for key, value in recipient.items()
        ):
            raise ValidationError(
                "Zgodovinski prejemnik ali DDV status se ne ujema s pogodbo."
            )
        if (
            not invoice.prejemnik_naziv.strip()
            or not invoice.prejemnik_naslov.strip()
            or not invoice.prejemnik_drzava_koda
            or (
                invoice.prejemnik_tip == "PRAVNA_OSEBA"
                and (
                    not invoice.prejemnik_davcna_stevilka
                    or not invoice.prejemnik_maticna_stevilka
                )
            )
            or type(invoice.izdajatelj_identificiran_za_ddv) is not bool
            or invoice.izdajatelj_identificiran_za_ddv != company.ddv_zavezanec
            or invoice.izdajatelj_drzava_koda != company.drzava_koda
            or not invoice.izdajatelj_davcna_stevilka
            or invoice.izdajatelj_davcna_stevilka != company.davcna_stevilka
        ):
            raise ValidationError("Manjkajo popolna in skladna dokazila o partnerjih.")
        fields = {
            "ddv_rezim_sifra": regime.sifra,
            "ddv_nacin_obracuna": regime.nacin_obracuna,
            "ddv_stopnja": regime.stopnja,
            "ddv_klavzula": regime.klavzula,
            "ddv_rekap_skupina": regime.rekapitulacijska_skupina,
            "ddv_zahteva_polni_odbitek": regime.zahteva_polni_odbitek,
            "ddv_eslog_koda": regime.eslog_koda
            or ESLOG_KODE_DDV.get(regime.nacin_obracuna),
        }
        if any(getattr(line, key) != value for key, value in fields.items()):
            raise ValidationError(
                "Zgodovinska DDV obravnava je spremenjena ali negotova."
            )
        if line.ddv_eslog_koda != ESLOG_KODE_DDV.get(line.ddv_nacin_obracuna):
            raise ValidationError(
                "Zgodovinska eSLOG kategorija ni skladna z načinom DDV."
            )
        start = period["start"] if period else invoice.obdobje_od
        end = period["end"] if period else invoice.obdobje_do
        if (
            regime.velja_od > start
            or (regime.velja_do and regime.velja_do < end)
            or not regime.rekapitulacijska_skupina.strip()
            or regime.stopnja < 0
            or regime.nacin_obracuna not in VatRegime.NacinObracuna.values
            or (
                regime.nacin_obracuna in {"OPROSCENO", "OBRNJENA_DAVCNA"}
                and not regime.klavzula.strip()
            )
        ):
            raise ValidationError(
                "DDV režim ni preverjen za celotno zgodovinsko obdobje."
            )
        if regime.zahteva_referenco:
            raise ValidationError(
                "Manjka strukturirano zgodovinsko dokazilo o obvezni DDV listini; potrebna je uskladitev."
            )
        expected_vat = (
            (line.osnova * line.ddv_stopnja / Decimal(100)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            if line.ddv_nacin_obracuna == "OBDAVCENO"
            else Decimal("0.00")
        )
        if (
            line.ddv_znesek != expected_vat
            or line.skupaj != line.osnova + line.ddv_znesek
        ):
            raise ValidationError(
                "Zgodovinski DDV izračun ni skladen s posnetno osnovo."
            )
    except ValidationError as exc:
        return [_reason("VAT_UNVERIFIED", "; ".join(exc.messages))]
    return []


def _build(event, line, business_month, verified):
    invoice, item = line.invoice, event.lease_item
    reasons = []
    if not invoice.stevilka or invoice.status in {
        "OSNUTEK",
        "ZA_PONOVNI_PREGLED",
        "ARHIVIRAN_NI_IZDAN",
    }:
        reasons.append(
            _reason(
                "ISSUANCE_UNVERIFIED",
                "Izdaja izvirnika ni dokazana z usklajeno številko in statusom.",
            )
        )
    period = _period(line, event, verified)
    if period is None:
        reasons.append(
            _reason(
                "PERIOD_UNVERIFIED",
                "Obdobje ni preverjeno; potrebni so datumi storitve in vir dokazil.",
            )
        )
    if (
        invoice.lease_id != item.lease_id
        or line.lease_id != item.lease_id
        or invoice.company_id != item.lease.unit.property.company_id
    ):
        reasons.append(
            _reason(
                "COMPANY_OR_LEASE_MISMATCH",
                "Izvirnik in postavka ne pripadata istemu podjetju in pogodbi.",
            )
        )
    corrections = _corrections(line)
    if (
        corrections
        or invoice.status in {"STORNIRAN", "POPRAVLJEN"}
        or invoice.povezan_racun_id
    ):
        reasons.append(
            _reason(
                "CORRECTION_HISTORY",
                "Storno, dobropis ali popravek zahteva izrecno uskladitev; samodejni poračun ni dovoljen.",
            )
        )
    if line.razlog_rocnega_popravka or line.izvirno_izracunana_vrednost is not None:
        reasons.append(
            _reason(
                "MANUAL_CORRECTION",
                "Ročni popravek izvirne vrstice zahteva uskladitev.",
            )
        )
    overlapping = InvoiceLine.objects.filter(
        lease_item_id=item.pk,
        invoice__document_type="NP",
        invoice__stevilka__isnull=False,
        invoice__obdobje_od__lte=invoice.obdobje_do,
        invoice__obdobje_do__gte=invoice.obdobje_od,
    ).exclude(pk=line.pk)
    if overlapping.exists():
        reasons.append(
            _reason(
                "AMBIGUOUS_SOURCE",
                "Več izdanih vrstic za isto postavko in obdobje zahteva uskladitev.",
            )
        )
    amount, calculation = None, None
    if period:
        days = (period["end"] - period["start"]).days + 1
        new_net = (
            event.nova_vrednost * Decimal(days) / Decimal(period["month_days"])
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        old_net = (
            event.stara_vrednost * Decimal(days) / Decimal(period["month_days"])
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        amount = new_net - line.osnova
        calculation = {
            "new_monthly_net": event.nova_vrednost,
            "old_monthly_net": event.stara_vrednost,
            "calendar_days": days,
            "month_days": period["month_days"],
            "rounding": "ROUND_HALF_UP",
            "new_period_net": new_net,
            "expected_old_period_net": old_net,
            "billed_original_net": line.osnova,
            "adjustment_net": amount,
        }
        if line.osnova != old_net:
            reasons.append(
                _reason(
                    "HISTORICAL_NET_MISMATCH",
                    "Izvirna zaračunana osnova se ne ujema s pričakovano zgodovinsko najemnino za obdobje; potrebna je izrecna zgodovinska uskladitev.",
                )
                | {
                    "expected_old_period_net": str(old_net),
                    "billed_original_net": str(line.osnova),
                    "difference": str(line.osnova - old_net),
                }
            )
        if amount < 0:
            reasons.append(
                _reason(
                    "NEGATIVE_ADJUSTMENT",
                    "Negativni poračun zahteva ročni pregled; ostane nerazrešen brez samodejnega vključevanja ali dobropisa.",
                )
                | {"amount": str(amount)}
            )
    if event.izkljucen or event.nova_vrednost <= 0 or event.stara_vrednost <= 0:
        reasons.append(
            _reason(
                "EVENT_UNVERIFIED",
                "Indeksacijski dogodek ni veljaven za samodejni poračun.",
            )
        )
    try:
        from .services import najemnina_na_datum

        if (
            najemnina_na_datum(item, event.datum_uskladitve - timedelta(days=1))
            != event.stara_vrednost
        ):
            raise ValidationError(
                "Stara vrednost dogodka se ne ujema s preverjeno zgodovino."
            )
        najemnina_na_datum(item, invoice.obdobje_do)
    except ValidationError as exc:
        reasons.append(_reason("RENT_HISTORY_UNVERIFIED", "; ".join(exc.messages)))
    if IndexationEvent.objects.filter(
        lease_item=item,
        izkljucen=False,
        datum_uskladitve__gt=event.datum_uskladitve,
        datum_uskladitve__lte=invoice.obdobje_do,
    ).exists():
        reasons.append(
            _reason(
                "EVENT_UNVERIFIED",
                "Obdobje vsebuje kasnejšo indeksacijo; potrebna je uskladitev.",
            )
        )
    reasons.extend(_vat_errors(line, period))
    evidence = _json(
        {
            "schema_version": 1,
            "event": {
                "id": event.pk,
                "lease_item_id": item.pk,
                "effective_date": event.datum_uskladitve,
                "old_net": event.stara_vrednost,
                "new_net": event.nova_vrednost,
                "index": event.indeks_vrednost,
            },
            "lease": {
                "id": item.lease_id,
                "company_id": item.lease.unit.property.company_id,
                "reference": item.lease.oznaka_pogodbe,
            },
            "source_invoice": {
                field.name: getattr(invoice, field.attname)
                for field in Invoice._meta.concrete_fields
                if field.name.startswith(("prejemnik_", "izdajatelj_"))
                or field.name
                in {
                    "id",
                    "company",
                    "lease",
                    "document_type",
                    "stevilka",
                    "obdobje_od",
                    "obdobje_do",
                    "povezan_racun",
                }
            },
            "source_line": {
                field.name: getattr(line, field.attname)
                for field in InvoiceLine._meta.concrete_fields
            },
            "period": period,
            "calculation": calculation,
            "corrections": corrections,
            "overlapping_line_ids": list(
                overlapping.order_by("pk").values_list("pk", flat=True)
            ),
            "validation_context": {
                "regime": {
                    field.name: getattr(item.vat_regime, field.attname)
                    for field in VatRegime._meta.concrete_fields
                },
                "recipient": {
                    field: getattr(item.lease.tenant, field)
                    for field in (
                        "tip",
                        "naziv",
                        "naslov",
                        "drzava_koda",
                        "davcna_stevilka",
                        "ddv_identifikacijska_stevilka",
                        "maticna_stevilka",
                        "identificiran_za_ddv",
                        "ddv_zavezanec_s_polnim_odbitkom",
                    )
                },
                "issuer": {
                    field: getattr(item.lease.unit.property.company, field)
                    for field in (
                        "ddv_zavezanec",
                        "drzava_koda",
                        "davcna_stevilka",
                    )
                },
                "rent_reference": {
                    "amount": item.referencna_najemnina,
                    "date": item.datum_referencne_najemnine,
                },
                "rent_events": list(
                    IndexationEvent.objects.filter(lease_item=item)
                    .order_by("datum_uskladitve", "pk")
                    .values()
                ),
                "rent_audits": list(
                    AuditLogEntry.objects.filter(
                        content_type=ContentType.objects.get_for_model(item),
                        object_id=item.pk,
                        polje="znesek_neto_mesecno",
                    )
                    .order_by("pk")
                    .values()
                ),
            },
            "target_policy": {
                "company_id": item.lease.unit.property.company_id,
                "lease_id": item.lease_id,
                "document_type": "NP",
                "future_only": True,
                "adjustment_only": False,
            },
        }
    )
    state = (
        "MANUAL_REVIEW"
        if amount is not None and amount < 0
        else "UNRESOLVED"
        if reasons
        else "VERIFIED"
    )
    return PoracunIndeksacije(
        indexation_event=event,
        original_invoice=invoice,
        source_line=line,
        prizadeto_obdobje_od=period["start"] if period else invoice.obdobje_od,
        prizadeto_obdobje_do=period["end"] if period else invoice.obdobje_do,
        znesek_poracuna=amount,
        calculation_evidence=evidence,
        detection_month=business_month,
        detected_at=timezone.now(),
        evidence_state=state,
        unresolved_reasons=reasons,
        last_observation_fingerprint=_fingerprint(
            {"evidence": evidence, "reasons": reasons}
        ),
    )


def _sources(event, business_month):
    end = date(
        business_month.year,
        business_month.month,
        monthrange(business_month.year, business_month.month)[1],
    )
    return (
        InvoiceLine.objects.filter(
            Q(invoice__stevilka__isnull=False) & ~Q(invoice__stevilka="")
            | Q(
                invoice__status__in=[
                    "IZBRAN_ZA_POSILJANJE",
                    "POSLAN",
                    "NAPAKA",
                    "REZULTAT_NI_ZNAN",
                    "STORNIRAN",
                    "POPRAVLJEN",
                ]
            ),
            lease_item_id=event.lease_item_id,
            invoice__document_type="NP",
            invoice__obdobje_do__gte=event.datum_uskladitve,
            invoice__obdobje_od__lte=end,
        )
        .select_related(
            "invoice",
            "lease_item__vat_regime",
            "lease_item__lease__tenant",
            "lease_item__lease__unit__property__company",
        )
        .order_by("invoice__obdobje_od", "invoice_id", "pk")
    )


def _month(value):
    if type(value) is not date or value.day != 1:
        raise ValidationError(
            "Poslovni mesec mora biti izrecen datum prvega dne meseca."
        )
    return value


def prepare_adjustments(event, *, business_month, verified_periods=None):
    """Compatibility proposal builder; caller must persist and audit atomically."""
    _month(business_month)
    proposals = []
    for line in _sources(event, business_month):
        if (
            PoracunIndeksacije.objects.filter(indexation_event=event)
            .filter(
                Q(source_line=line)
                | Q(source_line__isnull=True, original_invoice=line.invoice)
            )
            .exists()
        ):
            continue
        proposals.append(_build(event, line, business_month, verified_periods or {}))
    return proposals


def _observation_values(row):
    from .models import _ADJUSTMENT_OBSERVATION_FIELDS

    return {
        field: deepcopy(getattr(row, field))
        for field in sorted(_ADJUSTMENT_OBSERVATION_FIELDS)
    }


def _audit(row, previous, observation):
    current = _observation_values(row)
    audit = AuditLogEntry(
        content_type=ContentType.objects.get_for_model(row),
        object_id=row.pk,
        polje="last_observation_fingerprint",
        stara_vrednost=previous.get("last_observation_fingerprint", ""),
        nova_vrednost=row.last_observation_fingerprint,
        razlog=json.dumps(
            {
                "event": "INDEXATION_EVIDENCE_OBSERVATION",
                "reasons": row.unresolved_reasons,
                "observation": observation,
                "changes": {
                    field: {"old": previous.get(field), "new": value}
                    for field, value in current.items()
                    if previous.get(field) != value
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
    )
    audit.full_clean()
    audit.save()


@transaction.atomic
def persist_proposal(row):
    row.full_clean()
    row.save()
    return row


@transaction.atomic
def save_adjustment(row, *args, **kwargs):
    """Ordinary saves cannot change evidence; sourced creation always has an audit."""
    old = PoracunIndeksacije.objects.filter(pk=row.pk).first() if row.pk else None
    validate_adjustment_write(row, previous=old)
    if old is None and row.source_line_id:
        row.full_clean(validate_unique=False, validate_constraints=False)
        if row.last_observation_fingerprint != _fingerprint(
            {
                "evidence": row.calculation_evidence,
                "reasons": row.unresolved_reasons,
            }
        ):
            raise ValidationError("Odtis prvega opazovanja se ne ujema z dokazili.")
    result = models.Model.save(row, *args, **kwargs)
    if old is None and row.source_line_id:
        _audit(row, {"last_observation_fingerprint": ""}, row.calculation_evidence)
    return result


def validate_bulk_adjustments(rows, *, update_conflicts):
    """Legacy imports are validated; sourced rows use the audited save service."""
    if update_conflicts:
        raise ValidationError("Paketno prepisovanje zgodovine poračunov ni dovoljeno.")
    for row in rows:
        validate_adjustment_write(row)
        row.full_clean()
        if row.source_line_id:
            raise ValidationError(
                "Izvorne poračune shranite prek revidirane domenske storitve, ne bulk_create."
            )


def _record_observation(row, candidate):
    """Called only inside atomic detection. Preserve every prior reason verbatim."""
    previous = _observation_values(row)
    combined = deepcopy(row.unresolved_reasons)
    additions = deepcopy(candidate.unresolved_reasons)
    if candidate.calculation_evidence != row.calculation_evidence:
        additions.append(
            _reason(
                "EVIDENCE_CHANGED",
                "Dokazila so se spremenila; prvotni znesek in odločitev ostaneta ohranjena do izrecne uskladitve.",
            )
        )
    for reason in additions:
        if reason not in combined:
            combined.append(reason)
    row.unresolved_reasons = combined
    row.evidence_state = (
        "MANUAL_REVIEW"
        if any(reason["code"] == "NEGATIVE_ADJUSTMENT" for reason in combined)
        else "UNRESOLVED"
    )
    row.last_observation_fingerprint = candidate.last_observation_fingerprint
    validate_adjustment(row)
    # The ordinary save hook rejects observation edits. This private writer and
    # its audit share the detection transaction; no public bypass flag exists.
    models.Model.save(
        row,
        update_fields=[
            "evidence_state",
            "unresolved_reasons",
            "last_observation_fingerprint",
        ],
    )
    _audit(row, previous, candidate.calculation_evidence)


@transaction.atomic
def detect_adjustments(company_id, event_id, *, business_month, verified_periods=None):
    """Persist once per event/source line, including issued current-month sources.

    verified_periods maps source line IDs to start/end dates and a documentary
    reference. Supplying it asserts explicit historical verification. A retry
    never replaces earlier evidence, amounts, decisions or unresolved exceptions.
    """
    business_month = _month(business_month)
    event = IndexationEvent.objects.select_related(
        "lease_item__lease__unit__property__company"
    ).get(
        pk=event_id,
        lease_item__lease__unit__property__company_id=company_id,
    )
    rows, seen = [], set()
    for line in _sources(event, business_month):
        legacy = PoracunIndeksacije.objects.filter(
            indexation_event=event,
            original_invoice=line.invoice,
            source_line__isnull=True,
        ).order_by("pk")
        if legacy.exists():
            for row in legacy:
                if row.pk not in seen:
                    rows.append(row)
                    seen.add(row.pk)
            continue  # Never infer a legacy line, amount or decision.
        candidate = _build(event, line, business_month, verified_periods or {})
        row = PoracunIndeksacije.objects.filter(
            indexation_event=event, source_line=line
        ).first()
        if row is None:
            row = persist_proposal(candidate)
        elif candidate.last_observation_fingerprint != row.last_observation_fingerprint:
            _record_observation(row, candidate)
        rows.append(row)
    return rows


def validate_adjustment(row):
    if row.status == "ZAVRNJEN" and not row.razlog_zavrnitve.strip():
        raise ValidationError({"razlog_zavrnitve": "Razlog zavrnitve je obvezen."})
    if (
        row.original_invoice_id
        and not row.original_invoice.stevilka
        and not (
            row.source_line_id
            and row.evidence_state != "VERIFIED"
            and "ISSUANCE_UNVERIFIED"
            in {reason["code"] for reason in row.unresolved_reasons}
        )
    ):
        raise ValidationError({"original_invoice": "Originalni račun mora biti izdan."})
    if not row.source_line_id and row.evidence_state != "LEGACY_UNVERIFIED":
        raise ValidationError(
            "Poračun brez dokazila izvorne vrstice ostane zgodovinsko nepreverjen."
        )
    if row.source_line_id:
        if (
            not row.calculation_evidence
            or row.detection_month is None
            or row.detected_at is None
        ):
            raise ValidationError("Manjkajo dokazila izvora in zaznave poračuna.")
        if (
            row.calculation_evidence.get("schema_version") != 1
            or row.calculation_evidence.get("source_line", {}).get("id")
            != row.source_line_id
            or row.calculation_evidence.get("source_invoice", {}).get("id")
            != row.original_invoice_id
            or row.calculation_evidence.get("event", {}).get("id")
            != row.indexation_event_id
        ):
            raise ValidationError(
                "Posnetna identiteta dokazil se ne ujema z virom poračuna."
            )
        if (
            row.source_line.invoice_id != row.original_invoice_id
            or row.source_line.lease_item_id != row.indexation_event.lease_item_id
        ):
            raise ValidationError("Vir poračuna se ne ujema z izvirnikom in dogodkom.")
        calculation = row.calculation_evidence.get("calculation")
        if calculation is not None and row.znesek_poracuna != Decimal(
            calculation["adjustment_net"]
        ):
            raise ValidationError(
                {"znesek_poracuna": "Znesek se ne ujema s shranjenim izračunom."}
            )
        if row.evidence_state == "VERIFIED" and (
            row.znesek_poracuna is None
            or row.znesek_poracuna < 0
            or row.unresolved_reasons
        ):
            raise ValidationError(
                "Nerazrešen poračun ne more imeti preverjenega stanja."
            )
        if (
            row.znesek_poracuna is not None
            and row.znesek_poracuna < 0
            and (
                row.evidence_state != "MANUAL_REVIEW"
                or "NEGATIVE_ADJUSTMENT"
                not in {reason["code"] for reason in row.unresolved_reasons}
            )
        ):
            raise ValidationError(
                "Negativni poračun zahteva ročni pregled in ohranitev opozorila."
            )


def validate_adjustment_write(row, *, previous=None):
    old = previous
    if old is None and row.pk:
        old = PoracunIndeksacije.objects.filter(pk=row.pk).first()
    if old is not None:
        from .models import _ADJUSTMENT_EVIDENCE_FIELDS, _ADJUSTMENT_OBSERVATION_FIELDS

        if any(
            getattr(old, field) != getattr(row, field)
            for field in _ADJUSTMENT_EVIDENCE_FIELDS
            if not field.endswith("_id")
            and field not in {"source_line", "original_invoice", "indexation_event"}
        ):
            raise ValidationError("Dokazila poračuna so nespremenljiva.")
        if (old.source_line_id, old.original_invoice_id, old.indexation_event_id) != (
            row.source_line_id,
            row.original_invoice_id,
            row.indexation_event_id,
        ):
            raise ValidationError("Dokazila poračuna so nespremenljiva.")
        if any(
            getattr(old, field) != getattr(row, field)
            for field in _ADJUSTMENT_OBSERVATION_FIELDS
        ):
            raise ValidationError(
                "Dokazila opazovanja so nespremenljiva brez revidirane domenske storitve."
            )
    if row.source_line_id and row.vkljucen_v_racun_id:
        raise ValidationError("Samodejno vključevanje poračunov še ni omogočeno.")
    if row.source_line_id:
        validate_adjustment(row)


def unresolved_warnings(company_id):
    rows = (
        PoracunIndeksacije.objects.filter(
            indexation_event__lease_item__lease__unit__property__company_id=company_id,
        )
        .exclude(evidence_state="VERIFIED")
        .select_related("indexation_event__lease_item__lease")
        .order_by("pk")
    )
    warnings = []
    for row in rows:
        lease = (
            row.calculation_evidence.get("lease", {}).get("reference")
            or row.indexation_event.lease_item.lease.oznaka_pogodbe
        )
        context = f"Pogodba {lease} (#{row.indexation_event.lease_item.lease_id}), obdobje {row.prizadeto_obdobje_od:%d.%m.%Y}–{row.prizadeto_obdobje_do:%d.%m.%Y}"
        negative_amounts = list(
            dict.fromkeys(
                Decimal(reason["amount"])
                for reason in row.unresolved_reasons
                if reason["code"] == "NEGATIVE_ADJUSTMENT" and "amount" in reason
            )
        )
        if (
            row.znesek_poracuna is not None
            and row.znesek_poracuna < 0
            and row.znesek_poracuna not in negative_amounts
        ):
            negative_amounts.append(row.znesek_poracuna)
        for negative_amount in negative_amounts:
            warnings.append(
                f"{context}: negativni poračun {negative_amount:.2f} EUR zahteva ročni pregled. Ostaja nerazrešen; brez samodejnega vključevanja v NP račun ali izdaje dobropisa."
            )
        if not negative_amounts:
            warnings.append(
                f"{context}: dokazila poračuna niso razrešena. Potrebna je izrecna uskladitev."
            )
    return warnings
