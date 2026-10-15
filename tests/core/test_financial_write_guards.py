from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db.models import F

from catalogs.models import VatRegime
from core.services import record_edit_fingerprint
from tests.core import test_financial_audit
from tests.ui.test_contract_leases import make_hierarchy, make_vat

edit_case = test_financial_audit.edit_case


@pytest.mark.django_db
@pytest.mark.parametrize("path", ["save", "partial_save", "force_update", "update", "bulk_update", "upsert"])
def test_ordinary_financial_writes_are_rejected(edit_case, path):
    record, _, _, changes, _ = edit_case
    source = record_edit_fingerprint(record)
    test_financial_audit.change(record, changes)
    field = next(iter(changes))
    with pytest.raises(ValidationError):
        if path == "save":
            record.save()
        elif path == "partial_save":
            record.save(update_fields=[field])
        elif path == "force_update":
            record.save(force_update=True)
        elif path == "update":
            type(record).objects.filter(pk=record.pk).update(**changes)
        elif path == "bulk_update":
            type(record).objects.bulk_update([record], list(changes))
        else:
            type(record).objects.bulk_create([record], update_conflicts=True, update_fields=list(changes), unique_fields=["id"])
    record.refresh_from_db()
    assert record_edit_fingerprint(record) == source
    assert not test_financial_audit.audits(record).exists()


@pytest.mark.django_db
def test_ordinary_noop_save_still_works(edit_case):
    record, _, _, _, _ = edit_case
    source = record_edit_fingerprint(record)
    record.save()
    assert record_edit_fingerprint(record) == source
    assert not test_financial_audit.audits(record).exists()


@pytest.mark.django_db
def test_queryset_expression_cannot_change_vat_rate():
    record = make_vat()
    with pytest.raises(ValidationError):
        VatRegime.objects.filter(pk=record.pk).update(stopnja=F("stopnja") + Decimal(1))


@pytest.mark.django_db
def test_partial_nonfinancial_company_save_does_not_write_dirty_financial_fields():
    record = make_hierarchy().unit.property.company
    original = record.naziv
    record.naziv = "Unsaved financial change"
    record.email_posiljatelj = "new@example.com"
    record.save(update_fields=["email_posiljatelj"])
    record.refresh_from_db()
    assert record.naziv == original and record.email_posiljatelj == "new@example.com"
    assert not test_financial_audit.audits(record).exists()


@pytest.mark.django_db
def test_foreign_key_alias_update_is_rejected():
    lease = make_hierarchy()
    with pytest.raises(ValidationError):
        type(lease).objects.filter(pk=lease.pk).update(tenant_id=lease.tenant_id)
    with pytest.raises(ValidationError):
        type(lease).objects.bulk_update([lease], ["tenant_id"])
