"""Explicit historical/corrupt financial states for regression fixtures."""

from django.db import models


def save_financial_fixture(record, **kwargs):
    assert record._meta.label_lower in {"companies.company", "contracts.lease", "contracts.leaseitem", "catalogs.vatregime"}
    if record.pk is None:
        return record.save(**kwargs)
    return models.Model.save(record, **kwargs)


def update_financial_fixture(queryset, **changes):
    assert queryset.model._meta.label_lower in {"companies.company", "contracts.lease", "contracts.leaseitem", "catalogs.vatregime"}
    return models.QuerySet.update(queryset, **changes)
