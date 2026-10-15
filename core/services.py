import json
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder

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
    }
    if changes and (not isinstance(reason, str) or not reason.strip()):
        raise ValidationError({"razlog": "Razlog spremembe je obvezen."})
    return changes


def audit_field_changes(record, previous_values, reason):
    for name, old in previous_values.items():
        new = getattr(record, record._meta.get_field(name).attname)
        if old != new:
            entry = AuditLogEntry(
                content_type=ContentType.objects.get_for_model(record), object_id=record.pk,
                polje=name, stara_vrednost=str(old), nova_vrednost=str(new),
                razlog=reason.strip(),
            )
            entry.full_clean()
            entry.save()


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
