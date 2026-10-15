"""Audited business edits and explicit legacy/corrupt regression fixtures."""

from django.db import models, transaction

from catalogs.models import ServiceCatalogItem
from catalogs.services import deaktiviraj_storitev, ponovno_aktiviraj_storitev
from companies.models import Signatory
from contracts.models import Property, RecipientContact, Tenant, Unit
from contracts.services import (
    arhiviraj_poslovni_zapis,
    deaktiviraj_prejemnika,
    ponovno_aktiviraj_poslovni_zapis,
    ponovno_aktiviraj_prejemnika,
)
from core.services import record_edit_fingerprint

BUSINESS_MODELS = (Tenant, Property, Unit, RecipientContact, ServiceCatalogItem, Signatory)


@transaction.atomic
def save_business_record(record, **kwargs):
    """Use production validation and audit services for a valid test edit."""
    assert type(record) in BUSINESS_MODELS
    if record.pk is None:
        return record.save(**kwargs)
    previous = type(record).objects.get(pk=record.pk)
    if kwargs.get("update_fields") is not None:
        for field in record._meta.concrete_fields:
            if field.name not in kwargs["update_fields"] and field.attname not in kwargs["update_fields"]:
                setattr(record, field.attname, getattr(previous, field.attname))
    field = next((name for name in ("aktiven", "aktivna") if hasattr(record, name)), None)
    if field and getattr(record, field) != getattr(previous, field):
        active = getattr(record, field)
        if isinstance(record, RecipientContact):
            result = (ponovno_aktiviraj_prejemnika if active else deaktiviraj_prejemnika)(previous)
        elif isinstance(record, ServiceCatalogItem):
            result = (ponovno_aktiviraj_storitev if active else deaktiviraj_storitev)(record.pk)
        else:
            result = (ponovno_aktiviraj_poslovni_zapis if active else arhiviraj_poslovni_zapis)(previous)
        assert result.uspesno, result.razlogi
        previous.refresh_from_db()
    record._business_edit = (record_edit_fingerprint(previous), "Regression test business edit")
    return record.save()


def update_business_records(queryset, **changes):
    assert queryset.model in BUSINESS_MODELS
    for record in queryset:
        for name, value in changes.items():
            setattr(record, name, value)
        save_business_record(record)


def save_business_fixture(record, **kwargs):
    """Construct an explicitly historical or invalid state, never a UI edit."""
    assert type(record) in BUSINESS_MODELS
    return models.Model.save(record, **kwargs)


def update_business_fixture(queryset, **changes):
    """Construct an explicitly historical or invalid state for validation tests."""
    assert queryset.model in BUSINESS_MODELS
    return models.QuerySet.update(queryset, **changes)
