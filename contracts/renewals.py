"""Explicit automatic renewal decisions; reads never extend a lease."""

from calendar import monthrange
from datetime import date

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from core.models import AuditLogEntry
from core.services import (
    audit_field_changes,
    financial_changes,
    record_edit_fingerprint,
    validate_edit_source,
)

from .models import Lease, LeaseRenewalEvent
from .services import hierarhija_pogodbe_je_uporabna


def lease_source_fingerprint(lease):
    return record_edit_fingerprint(lease)


LEASE_FINANCIAL_FIELDS = tuple(
    field.name for field in Lease._meta.concrete_fields if field.name not in {"id", "arhivirana"}
)


def validate_lease_edit(previous, changes, *, source_fingerprint=None):
    if source_fingerprint is not None and source_fingerprint != lease_source_fingerprint(previous):
        raise ValidationError("Pogodba se je med urejanjem spremenila. Osvežite obrazec in preverite trenutne podatke.")
    if (
        previous.tip_trajanja == Lease.TipTrajanja.DOLOCEN_CAS
        and previous.samodejno_podaljsanje
        and previous.datum_konca
        and previous.datum_konca <= timezone.localdate()
    ):
        for field in ("datum_konca", "tip_trajanja", "samodejno_podaljsanje"):
            if field in changes and changes[field] != getattr(previous, field):
                raise ValidationError({field: (
                    "Po izteku pogodbe trajanja ni mogoče spremeniti z običajnim urejanjem. "
                    "Uporabite »Potrdi podaljšanje«. Pogodbeni aneks ni potrditev samodejnega podaljšanja."
                )})


@transaction.atomic
def save_lease_edit(lease, *, source_fingerprint, razlog=""):
    if not isinstance(source_fingerprint, str) or not source_fingerprint:
        raise ValidationError("Manjka veljaven pregled pogodbe. Osvežite obrazec.")
    previous = Lease.objects.select_for_update().get(pk=lease.pk)
    if lease.arhivirana != previous.arhivirana:
        raise ValidationError("Za arhiviranje uporabite namensko storitev pogodbe.")
    changes = {field.attname: getattr(lease, field.attname) for field in Lease._meta.fields}
    validate_lease_edit(previous, changes, source_fingerprint=source_fingerprint)
    validate_edit_source(previous, source_fingerprint)
    lease._state.fields_cache.clear()
    lease.clean_fields()
    lease.full_clean()
    changed = financial_changes(previous, lease, LEASE_FINANCIAL_FIELDS, razlog)
    models.Model.save(lease)
    lease.refresh_from_db()
    audit_field_changes(lease, changed, razlog)
    return lease


def renewal_due(lease, as_of):
    return bool(
        hierarhija_pogodbe_je_uporabna(lease)
        and lease.tip_trajanja == Lease.TipTrajanja.DOLOCEN_CAS
        and lease.samodejno_podaljsanje
        and lease.status == Lease.Status.AKTIVNA
        and not lease.datum_odpovedi
        and not lease.datum_prenehanja
        and lease.datum_konca
        and lease.datum_konca <= as_of
    )


def add_calendar_months(end, months):
    """Keep the day number, clipping to the target month's last day if needed."""
    if type(months) is not int or months <= 0:
        raise ValidationError("Obdobje podaljšanja mora biti pozitivno število mesecev.")
    year, month = divmod(end.year * 12 + end.month - 1 + months, 12)
    if not 1 <= year <= 9999:
        raise ValidationError("Predlagani datum podaljšanja je zunaj dovoljenega obsega.")
    month += 1
    return date(year, month, min(end.day, monthrange(year, month)[1]))


def renewal_proposal(lease, as_of):
    if not renewal_due(lease, as_of):
        return None
    new_end = add_calendar_months(lease.datum_konca, lease.obdobje_podaljsanja_mesecev)
    return {
        "old_end": lease.datum_konca.isoformat(),
        "new_end": new_end.isoformat(),
        "months": lease.obdobje_podaljsanja_mesecev,
        "source_fingerprint": lease_source_fingerprint(lease),
    }


@transaction.atomic
def confirm_renewal(lease_id, source_fingerprint, *, as_of):
    lease = Lease.objects.select_for_update().select_related(
        "unit__property__company", "tenant",
    ).get(pk=lease_id)
    # SQLite uses IMMEDIATE transactions. The unique old-end constraint is a
    # second guard; a successful retry returns the original immutable event.
    existing = LeaseRenewalEvent.objects.filter(
        lease=lease, source_fingerprint=source_fingerprint,
    ).first()
    if existing:
        return existing
    proposal = renewal_proposal(lease, as_of)
    if not proposal or proposal["source_fingerprint"] != source_fingerprint:
        raise ValidationError("Predlog podaljšanja ni več veljaven. Osvežite pregled pogodbe.")
    if LeaseRenewalEvent.objects.filter(lease=lease, old_end=lease.datum_konca).exists():
        raise ValidationError("Podaljšanje tega obdobja je že potrjeno.")
    old_end = lease.datum_konca
    new_end = date.fromisoformat(proposal["new_end"])
    lease.datum_konca = new_end
    lease.full_clean()
    # Ordinary saves cannot change expired renewal terms. This audited service
    # owns the exceptional write, in the same transaction as its evidence.
    models.Model.save(lease, update_fields=["datum_konca"])
    audit = AuditLogEntry.objects.create(
        content_type=ContentType.objects.get_for_model(Lease), object_id=lease.pk,
        polje="datum_konca", stara_vrednost=old_end.isoformat(),
        nova_vrednost=new_end.isoformat(), razlog="Izrecna potrditev samodejnega podaljšanja.",
    )
    return LeaseRenewalEvent.objects.create(
        lease=lease, old_end=old_end, new_end=new_end,
        months=proposal["months"], source_fingerprint=source_fingerprint,
        decided_at=timezone.now(), audit=audit,
    )


def require_confirmed_renewal(lease_id, period_end):
    lease = Lease.objects.select_for_update().select_related(
        "unit__property__company", "tenant",
    ).get(pk=lease_id)
    if renewal_due(lease, period_end):
        raise ValidationError("Pred izdajo računa izrecno potrdite podaljšanje pogodbe ali vnesite odpoved.")
