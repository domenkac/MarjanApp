"""Persistent January review. Each company batch is one atomic decision unit."""

import hashlib
import json
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.core.validators import DecimalValidator
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from companies.models import Company
from core.models import AuditLogEntry
from invoicing.models import Invoice, InvoiceLine, MonthlyBatch, MonthlyCycle

from .models import (
    IndexationBatch,
    IndexationDecision,
    IndexationEvent,
    Lease,
    LeaseItem,
    Property,
    Tenant,
    Unit,
)
from .services import (
    efektivna_veljavnost_pogodbe,
    efektivna_veljavnost_postavke,
    hierarhija_pogodbe_je_uporabna,
    izracunaj_indeksacijo,
    najemnina_na_datum,
    potrdi_indeksacijo,
)


def validate_input(index_year, index_value):
    if type(index_year) is not int or not 1 <= index_year <= 9998:
        raise ValidationError("Vnesite veljavno leto indeksa (1–9998).")
    if (
        not isinstance(index_value, Decimal)
        or not index_value.is_finite()
        or index_value <= 0
    ):
        raise ValidationError("Indeks mora biti pozitivna Decimal vrednost.")
    DecimalValidator(6, 3)(index_value)
    return date(index_year + 1, 2, 1)


def _fingerprint(company_id):
    """Hash complete source rows, including additions and billing/freeze changes."""
    leases = Lease.objects.filter(unit__property__company_id=company_id)
    items = LeaseItem.objects.filter(lease__in=leases)
    sources = [
        Company.objects.filter(pk=company_id),
        Property.objects.filter(company_id=company_id),
        Unit.objects.filter(property__company_id=company_id),
        Tenant.objects.filter(pk__in=leases.values("tenant_id")),
        leases,
        items,
        IndexationEvent.objects.filter(lease_item__in=items),
        Invoice.objects.filter(company_id=company_id),
        InvoiceLine.objects.filter(invoice__company_id=company_id),
        MonthlyBatch.objects.filter(company_id=company_id),
        MonthlyCycle.objects.filter(batches__company_id=company_id).distinct(),
    ]
    # VAT validation and manual rent history are source data too.
    from catalogs.models import VatRegime

    sources.append(VatRegime.objects.filter(pk__in=items.values("vat_regime_id")))
    item_type = ContentType.objects.get_for_model(LeaseItem)
    sources.append(
        AuditLogEntry.objects.filter(
            content_type=item_type, object_id__in=items.values("pk")
        )
    )
    serialized = json.dumps(
        [list(query.order_by("pk").values()) for query in sources],
        cls=DjangoJSONEncoder,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode()).hexdigest()


def _late_block(item, effective_date):
    issued_or_uncertain = Q(stevilka__isnull=False) | Q(
        status__in=[
            Invoice.Status.IZBRAN_ZA_POSILJANJE,
            Invoice.Status.POSLAN,
            Invoice.Status.NAPAKA,
            Invoice.Status.REZULTAT_NI_ZNAN,
            Invoice.Status.STORNIRAN,
            Invoice.Status.POPRAVLJEN,
        ]
    )
    if (
        Invoice.objects.filter(
            issued_or_uncertain,
            lease=item.lease,
            document_type=Invoice.DocumentType.NP,
            obdobje_do__gte=effective_date,
        ).exists()
        or InvoiceLine.objects.filter(
            lease_item=item,
            invoice__in=Invoice.objects.filter(issued_or_uncertain),
            invoice__document_type=Invoice.DocumentType.NP,
            invoice__obdobje_do__gte=effective_date,
        ).exists()
    ):
        return "Izdan račun v prizadetem obdobju: pozna indeksacija je blokirana do T2-C (varen poračun)."
    for batch in MonthlyBatch.objects.filter(
        company_id=item.lease.unit.property.company_id,
        cycle__trenutni_korak__gte=MonthlyCycle.Korak.POTRDITEV_PAKETA,
    ).select_related("cycle"):
        period_end = batch.cycle.obdobje.replace(day=28) + timedelta(days=4)
        period_end -= timedelta(days=period_end.day)
        if period_end >= effective_date and any(
            candidate.get("lease_id") == item.lease_id
            for candidate in batch.calculation_snapshot.get("candidates", [])
        ):
            return "Pogodba je v zamrznjenem paketu: potrditev je blokirana; vsebina paketa ostane nespremenjena."
    return ""


def _proposals(company, index_year, index_value):
    effective_date = validate_input(index_year, index_value)
    rows = []
    leases = (
        Lease.objects.filter(
            unit__property__company=company,
            arhivirana=False,
            status__in=[Lease.Status.AKTIVNA, Lease.Status.ODPOVEDANA],
        )
        .select_related("unit__property__company", "tenant")
        .order_by("pk")
    )
    for lease in leases:
        start, end = efektivna_veljavnost_pogodbe(lease)
        if start > effective_date or (end and end < effective_date):
            continue
        items = list(
            lease.leaseitem_set.filter(arhivirana=False)
            .select_related("vat_regime")
            .order_by("pk")
        )
        applicable = [
            item
            for item in items
            if (
                efektivna_veljavnost_postavke(item)[0] <= effective_date
                and (
                    efektivna_veljavnost_postavke(item)[1] is None
                    or efektivna_veljavnost_postavke(item)[1] >= effective_date
                )
            )
        ]
        for item in applicable or [None]:
            proposal = {
                "tenant": lease.tenant.naziv,
                "property": lease.unit.property.naziv,
                "unit": lease.unit.oznaka,
                "contract": lease.oznaka_pogodbe,
                "item": item.naziv_postavke if item else "Ni veljavnih postavk",
                "effective_date": str(effective_date),
                "eligible": False,
                "blocked": False,
                "old_rent": None,
                "new_rent": None,
                "percentage": None,
            }
            try:
                lease.full_clean()
                if not hierarhija_pogodbe_je_uporabna(lease):
                    raise ValidationError("Nadrejeni podatki pogodbe niso aktivni.")
                if item is None:
                    raise ValidationError(
                        "Pogodba nima veljavnih postavk za indeksacijo."
                    )
                item.full_clean()
                old_rent = najemnina_na_datum(item, effective_date - timedelta(days=1))
                proposal["old_rent"] = str(old_rent)
                calculated = izracunaj_indeksacijo(
                    item, index_value, timezone.localdate(), indeks_leto=index_year
                )
                proposal.update(
                    eligible=calculated.pogoj_izpolnjen,
                    explanation=calculated.razlog,
                    new_rent=str(calculated.nova_vrednost)
                    if calculated.nova_vrednost is not None
                    else None,
                    percentage=str(calculated.odstotek_spremembe)
                    if calculated.odstotek_spremembe is not None
                    else None,
                )
                if calculated.pogoj_izpolnjen:
                    late = _late_block(item, effective_date)
                    if late:
                        raise ValidationError(late)
                    if IndexationEvent.objects.filter(
                        lease_item=item,
                        izkljucen=False,
                        datum_uskladitve__gte=effective_date,
                    ).exists():
                        raise ValidationError(
                            "Podvojena indeksacija ali kasnejši odvisni dogodek."
                        )
            except ValidationError as exc:
                proposal.update(
                    eligible=False, blocked=True, explanation="; ".join(exc.messages)
                )
            rows.append((lease.pk, item.pk if item else None, proposal))
    return rows


@transaction.atomic
def create_batch(company_id, index_year, index_value):
    effective_date = validate_input(index_year, index_value)
    company = Company.objects.get(pk=company_id)
    if not company.aktivno:
        raise ValidationError("Podjetje ni aktivno.")
    fingerprint = _fingerprint(company.pk)
    existing = IndexationBatch.objects.filter(
        company=company, index_year=index_year, index_value=index_value,
        source_fingerprint=fingerprint, state=IndexationBatch.State.PENDING,
    ).order_by("pk").first()
    if existing:
        return existing
    rows = _proposals(company, index_year, index_value)
    batch = IndexationBatch.objects.create(
        company=company,
        index_year=index_year,
        index_value=index_value,
        effective_date=effective_date,
        source_fingerprint=fingerprint,
    )
    for lease_id, item_id, proposal in rows:
        IndexationDecision.objects.create(
            batch=batch,
            lease_id=lease_id,
            lease_item_id=item_id,
            proposal=proposal,
            selected=proposal["eligible"],
        )
    return batch


def batch_is_stale(batch):
    return (
        batch.state == IndexationBatch.State.PENDING
        and batch.source_fingerprint != _fingerprint(batch.company_id)
    )


def _pending(company_id, batch_id, revision):
    batch = IndexationBatch.objects.get(pk=batch_id, company_id=company_id)
    if batch.state != IndexationBatch.State.PENDING or batch.revision != revision:
        raise ValidationError(
            "Pregled je že spremenjen ali potrjen. Ponovno odprite paket."
        )
    if batch_is_stale(batch):
        raise ValidationError(
            "Predogled je zastarel. Ustvarite nov predogled in ponovno preglejte odločitve."
        )
    return batch


@transaction.atomic
def save_review(company_id, batch_id, revision, selected_ids, exclusions):
    batch = _pending(company_id, batch_id, revision)
    decisions = list(batch.decisions.all())
    if not set(selected_ids) <= {row.pk for row in decisions} or not set(
        exclusions
    ) <= {row.lease_id for row in decisions}:
        raise ValidationError("Izbira ne pripada temu podjetju in paketu.")
    for reason in exclusions.values():
        if not reason.strip():
            raise ValidationError("Razlog izključitve pogodbe je obvezen.")
    for row in decisions:
        row.exclusion_reason = exclusions.get(row.lease_id, "").strip()
        row.selected = row.pk in selected_ids and not row.exclusion_reason
        if row.selected and not row.proposal["eligible"]:
            raise ValidationError("Neveljavnega predloga ni mogoče izbrati.")
        row.save(update_fields=["selected", "exclusion_reason"])
    batch.revision += 1
    batch.reviewed_at = timezone.now()
    batch.save(update_fields=["revision", "reviewed_at"])
    return batch


@transaction.atomic
def confirm_batch(company_id, batch_id, revision):
    batch = IndexationBatch.objects.get(pk=batch_id, company_id=company_id)
    if batch.state == IndexationBatch.State.CONFIRMED:
        return batch  # Durable retry: never writes events or audit twice.
    conflicting = IndexationBatch.objects.filter(
        company_id=batch.company_id, index_year=batch.index_year,
        state=IndexationBatch.State.CONFIRMED,
    ).exclude(index_value=batch.index_value).order_by("pk").first()
    if conflicting:
        raise ValidationError(
            f"Za leto {batch.index_year} je za to podjetje že potrjen indeks "
            f"{conflicting.index_value}. Sprememba potrjenega objavljenega indeksa "
            "zahteva opredeljen postopek poslovnega popravka. Potrditev je blokirana; "
            "zgodovinski dogodki in odločitve ostanejo nespremenjeni."
        )
    batch = _pending(company_id, batch_id, revision)
    expected = _proposals(batch.company, batch.index_year, batch.index_value)
    decisions = list(batch.decisions.order_by("lease_id", "lease_item_id", "pk"))
    actual = [(row.lease_id, row.lease_item_id, row.proposal) for row in decisions]
    if actual != expected or batch.effective_date != validate_input(
        batch.index_year, batch.index_value
    ):
        raise ValidationError(
            "Predlogi so zastareli ali neveljavni. Ustvarite nov predogled."
        )
    if not decisions:
        raise ValidationError("Paket nima predlogov.")
    now = timezone.now()
    contract_reasons = {}
    for row in decisions:
        if row.exclusion_reason and not row.exclusion_reason.strip():
            raise ValidationError("Razlog izključitve pogodbe je obvezen.")
        previous_reason = contract_reasons.setdefault(
            row.lease_id, row.exclusion_reason
        )
        if previous_reason != row.exclusion_reason:
            raise ValidationError(
                "Izključitev mora veljati za vse postavke pogodbe z istim razlogom."
            )
        if row.proposal["eligible"] and not row.selected and not row.exclusion_reason:
            raise ValidationError(
                "Neizbran upravičen predlog zahteva izključitev pogodbe z razlogom, "
                "ki velja za vse njene postavke."
            )
    for row in decisions:
        if row.proposal["blocked"] and not row.exclusion_reason:
            raise ValidationError(
                "Blokirana pogodba zahteva odpravo podatkov ali izključitev z razlogom."
            )
        if row.exclusion_reason:
            row.outcome = IndexationDecision.Outcome.EXCLUDED
            row.reason = row.exclusion_reason
        elif row.selected:
            if not row.proposal["eligible"]:
                raise ValidationError("Predlog ne izpolnjuje pogojev.")
            row.event = potrdi_indeksacijo(
                row.lease_item,
                batch.index_value,
                timezone.localdate(),
                indeks_leto=batch.index_year,
            )
            row.outcome = IndexationDecision.Outcome.APPROVED
            row.reason = row.proposal["explanation"]
        else:
            row.outcome = IndexationDecision.Outcome.SKIPPED
            row.reason = row.proposal["explanation"]
        row.decided_at = now
        row.save(update_fields=["event", "outcome", "reason", "decided_at"])
        # This records the actual decision change, not a fabricated LeaseItem change.
        audit = AuditLogEntry(
            content_type=ContentType.objects.get_for_model(row),
            object_id=row.pk,
            polje="outcome",
            stara_vrednost=IndexationDecision.Outcome.PENDING,
            nova_vrednost=row.outcome,
            razlog=json.dumps(
                {
                    "reason": row.reason,
                    "index_year": batch.index_year,
                    "index_value": str(batch.index_value),
                    "proposal": row.proposal,
                    "event_id": row.event_id,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        audit.full_clean()
        audit.save()
    batch.state = IndexationBatch.State.CONFIRMED
    batch.confirmed_at = now
    batch.revision += 1
    batch.save(update_fields=["state", "confirmed_at", "revision"])
    return batch
