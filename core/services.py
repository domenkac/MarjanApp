import json
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models, transaction
from django.utils.crypto import salted_hmac

from .models import AuditLogEntry


def record_edit_fingerprint(record):
    values = {
        field.attname: getattr(record, field.attname)
        for field in record._meta.concrete_fields
    }
    values = {key: getattr(value, "name", value) for key, value in values.items()}
    values = {
        key: format(value.normalize(), "f") if isinstance(value, Decimal) else value
        for key, value in values.items()
    }
    values["audit_id"] = AuditLogEntry.objects.filter(
        content_type=ContentType.objects.get_for_model(record), object_id=record.pk,
    ).order_by("-pk").values_list("pk", flat=True).first()
    return sha256(json.dumps(
        values, cls=DjangoJSONEncoder, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def validate_edit_source(previous, source_fingerprint):
    if not source_fingerprint or source_fingerprint != record_edit_fingerprint(previous):
        raise ValidationError(
            "Zapis se je med urejanjem spremenil ali manjka pregled. "
            "Osvežite obrazec in preverite trenutne podatke."
        )


def financial_changes(previous, record, fields, reason):
    changes = {
        name: getattr(previous, record._meta.get_field(name).attname)
        for name in fields
        if getattr(previous, record._meta.get_field(name).attname)
        != getattr(record, record._meta.get_field(name).attname)
        or not getattr(getattr(record, record._meta.get_field(name).attname), "_committed", True)
    }
    if changes and (not isinstance(reason, str) or not reason.strip()):
        raise ValidationError({"razlog": "Razlog spremembe je obvezen."})
    return changes


def credential_reference_audit_value(value):
    """Keep even secret-bearing credential references out of audit records."""
    return "hmac-sha256:" + salted_hmac(
        "configuration-audit-reference-v1", str(value), algorithm="sha256",
    ).hexdigest()


def audit_field_changes(record, previous_values, reason, *, value_formatter=None):
    for name, old in previous_values.items():
        new = getattr(record, record._meta.get_field(name).attname)
        if old != new:
            old_value = value_formatter(name, old) if value_formatter else str(old)
            new_value = value_formatter(name, new) if value_formatter else str(new)
            entry = AuditLogEntry(
                content_type=ContentType.objects.get_for_model(record), object_id=record.pk,
                polje=name, stara_vrednost=old_value, nova_vrednost=new_value,
                razlog=reason.strip(),
            )
            entry.full_clean()
            entry.save()


@transaction.atomic
def save_business_edit(record, fields, *, source_fingerprint, razlog="", file_field=None):
    """Use the T6-A edit protocol for remaining business data."""
    previous = type(record).objects.select_for_update().filter(pk=record.pk).first()
    if previous is None:
        raise ValidationError("Zapis ne obstaja več. Osvežite obrazec.")
    validate_edit_source(previous, source_fingerprint)
    for field in record._meta.concrete_fields:
        if field.name not in fields and getattr(record, field.attname) != getattr(previous, field.attname):
            raise ValidationError("Za to spremembo uporabite namensko domensko storitev.")
    record._state.fields_cache.clear()
    exclude = set()
    upload = None
    if file_field:
        value = getattr(record, file_field)
        if value.name == getattr(previous, file_field).name and value._committed:
            exclude.add(file_field)
        if not value._committed:
            upload, upload_name = value, value.name
    record.full_clean(exclude=exclude)
    changes = financial_changes(previous, record, fields, razlog)
    if changes:
        try:
            models.Model.save(record)
            record.refresh_from_db()
            audit_field_changes(record, changes, razlog)
        except Exception:
            if upload is not None and upload._committed:
                # FileField writes are outside the DB transaction. Remove only
                # the newly generated asset and retain the upload for a retry.
                upload.storage.delete(upload.name)
                upload.name, upload._committed = upload_name, False
                setattr(record, file_field, upload)
            raise
    return record


@dataclass(frozen=True)
class StanjeRazlog:
    koda: str
    sporocilo: str


@dataclass(frozen=True)
class StanjeRezultat:
    uspesno: bool
    razlogi: tuple[StanjeRazlog, ...] = ()


@dataclass(frozen=True)
class IzbrisRazlog:
    koda: str
    sporocilo: str
    tip_povezave: str
    stevilo_povezav: int


@dataclass(frozen=True)
class IzbrisOdlocitev:
    dovoljeno: bool
    razlogi: tuple[IzbrisRazlog, ...] = ()
    mozno_arhiviranje: bool = False
