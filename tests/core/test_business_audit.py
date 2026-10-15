from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from threading import Barrier

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connections
from django.forms.models import model_to_dict

from catalogs.models import ServiceCatalogItem
from catalogs.services import (
    deaktiviraj_storitev,
    ponovno_aktiviraj_storitev,
    save_service_catalog_item_edit,
)
from companies.models import Company, Signatory
from companies.services import save_signatory_edit
from contracts.models import RecipientContact
from contracts.services import (
    arhiviraj_poslovni_zapis,
    deaktiviraj_prejemnika,
    ponovno_aktiviraj_poslovni_zapis,
    ponovno_aktiviraj_prejemnika,
    save_contract_data_edit,
    save_recipient_edit,
)
from core.models import AuditLogEntry
from core.services import record_edit_fingerprint
from tests.core.test_financial_audit import audits, change
from tests.image_data import slika_bytes
from tests.ui.test_contract_leases import make_hierarchy, make_vat
from ui.forms import (
    PropertyForm,
    RecipientContactForm,
    ServiceCatalogItemForm,
    SignatoryForm,
    TenantForm,
    UnitForm,
)


@pytest.fixture(params=["tenant", "property", "unit", "recipient", "service", "signatory"])
def business_case(request, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    lease = make_hierarchy()
    cases = {
        "tenant": (lease.tenant, save_contract_data_edit, TenantForm,
                   {"naziv": "Novo ime", "naslov": "Nov naslov"},
                   f"/pogodbe/najemniki/{lease.tenant_id}/"),
        "property": (lease.unit.property, save_contract_data_edit, PropertyForm,
                     {"naziv": "Nova stavba", "naslov": "Nov naslov"},
                     f"/pogodbe/nepremicnine/{lease.unit.property_id}/"),
        "unit": (lease.unit, save_contract_data_edit, UnitForm,
                 {"oznaka": "P2", "povrsina_m2": Decimal("25.50")},
                 f"/pogodbe/enote/{lease.unit_id}/"),
    }
    if request.param == "recipient":
        record = RecipientContact.objects.create(lease=lease, email="old@example.com", vloga="TO", kanal="PDF")
        return record, save_recipient_edit, RecipientContactForm, {"email": "new@example.com", "vloga": "CC"}, f"/pogodbe/prejemniki/{record.pk}/"
    if request.param == "service":
        record = ServiceCatalogItem.objects.create(naziv="Storitev", enota_mere="kos")
        return record, save_service_catalog_item_edit, ServiceCatalogItemForm, {"naziv": "Druga storitev", "privzeta_cena": Decimal("50.00")}, f"/nastavitve/storitve/{record.pk}/"
    if request.param == "signatory":
        record = Signatory.objects.create(company=lease.unit.property.company, ime_priimek="Ana Novak", funkcija="Direktorica", podpis_slika="old.png", velja_od=date(2020, 1, 1))
        return record, save_signatory_edit, SignatoryForm, {"ime_priimek": "Ana Kovač", "funkcija": "Prokuristka"}, f"/nastavitve/podpisniki/{record.pk}/"
    return cases[request.param]


def form_data(case, *, reason="Popravek", **changes):
    record, _, form_class, _, _ = case
    data = model_to_dict(record, fields=form_class._meta.fields)
    data.pop("podpis_slika", None)
    # ModelForm data uses empty strings for optional values.
    data = {key: "" if value is None else value for key, value in data.items()}
    return data | {"razlog": reason, "edit_fingerprint": record_edit_fingerprint(record)} | changes


@pytest.mark.django_db
def test_edits_audit_only_changed_fields_with_values_identity_reason_time(business_case):
    record, service, _, changes, _ = business_case
    token = record_edit_fingerprint(record)
    old = {name: str(getattr(record, name)) for name in changes}
    change(record, changes)
    service(record, source_fingerprint=token, razlog="  Popravek  ")
    entries = list(audits(record))
    assert {entry.polje: (entry.stara_vrednost, entry.nova_vrednost) for entry in entries} == {
        name: (old[name], str(getattr(record, name))) for name in changes
    }
    assert len(entries) == len(changes)
    assert all(entry.razlog == "Popravek" and entry.created_at for entry in entries)
    assert all(entry.content_type.model_class() is type(record) and entry.object_id == record.pk for entry in entries)


@pytest.mark.django_db
@pytest.mark.parametrize("reason", [None, "", "  "])
def test_reason_required_atomically(business_case, reason):
    record, service, form_class, changes, _ = business_case
    data = form_data(business_case, reason=reason, **changes)
    assert not form_class(data, instance=record).is_valid()
    # ModelForm mutates its instance; the stored source remains unchanged.
    token = record_edit_fingerprint(type(record).objects.get(pk=record.pk))
    change(record, changes)
    with pytest.raises(ValidationError, match="Razlog"):
        service(record, source_fingerprint=token, razlog=reason)
    record.refresh_from_db()
    assert record_edit_fingerprint(record) == token
    assert not audits(record).exists()


@pytest.mark.django_db
def test_creation_and_noops_need_no_reason_or_audit(business_case):
    record, service, form_class, _, _ = business_case
    assert "razlog" not in form_class().fields
    assert "edit_fingerprint" not in form_class().fields
    service(record, source_fingerprint=record_edit_fingerprint(record))
    form = form_class(form_data(business_case, reason=""), instance=record)
    assert form.is_valid(), form.errors
    form.save()
    record.save()
    assert not audits(record).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("deferred", [False, True])
def test_form_save_rechecks_fingerprint_and_rejects_stale_submission(business_case, deferred):
    record, service, form_class, changes, _ = business_case
    data = form_data(business_case, **changes)
    form = form_class(data, instance=record)
    assert form.is_valid(), form.errors
    fresh = type(record).objects.get(pk=record.pk)
    token = record_edit_fingerprint(fresh)
    change(fresh, changes)
    service(fresh, source_fingerprint=token, razlog="Drugi popravek")
    assert not form_class(data, instance=type(record).objects.get(pk=record.pk)).is_valid()
    with pytest.raises(ValidationError, match="Osvežite"):
        form.save(commit=False).save() if deferred else form.save()
    assert audits(record).count() == len(changes)


@pytest.mark.django_db
@pytest.mark.parametrize("token", ["", "forged"])
def test_missing_or_forged_fingerprint_rejected(business_case, token):
    record, service, form_class, changes, _ = business_case
    data = form_data(business_case, **changes) | {"edit_fingerprint": token}
    assert not form_class(data, instance=record).is_valid()
    change(record, changes)
    with pytest.raises(ValidationError):
        service(record, source_fingerprint=token, razlog="Popravek")
    assert not audits(record).exists()


@pytest.mark.django_db
def test_deleted_record_rejects_stale_service_and_form():
    record = ServiceCatalogItem.objects.create(naziv="Storitev", enota_mere="kos")
    token = record_edit_fingerprint(record)
    ServiceCatalogItem.objects.filter(pk=record.pk).delete()
    with pytest.raises(ValidationError, match="Osvežite"):
        save_service_catalog_item_edit(record, source_fingerprint=token, razlog="Popravek")
    form = ServiceCatalogItemForm({"naziv": "Novo", "enota_mere": "kos", "edit_fingerprint": token, "razlog": "Popravek"}, instance=record)
    assert not form.is_valid()
    assert "Osvežite" in str(form.errors)


@pytest.mark.django_db
def test_queryset_guards_cover_every_business_field_and_foreign_key_alias(business_case):
    record, _, _, _, _ = business_case
    for field in record._meta.concrete_fields:
        if field.primary_key or field.name == "podpis_checksum":
            continue
        for name in {field.name, field.attname}:
            with pytest.raises(ValidationError):
                type(record).objects.filter(pk=record.pk).update(**{name: getattr(record, field.attname)})
            with pytest.raises(ValidationError):
                type(record).objects.bulk_update([record], [name])


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["property", "service"])
def test_foreign_key_edits_audit_ids_without_related_personal_data(kind):
    if kind == "property":
        record = make_hierarchy().unit.property
        field, service = "company", save_contract_data_edit
        replacement = Company.objects.create(naziv="Drugo podjetje", aktivno=False)
    else:
        record = ServiceCatalogItem.objects.create(naziv="Storitev", enota_mere="kos")
        field, service = "privzeti_ddv_rezim", save_service_catalog_item_edit
        replacement = make_vat()
    token = record_edit_fingerprint(record)
    old = getattr(record, f"{field}_id")
    setattr(record, field, replacement)
    service(record, source_fingerprint=token, razlog="Popravek povezave")
    entry = audits(record).get()
    assert (entry.polje, entry.stara_vrednost, entry.nova_vrednost) == (field, str(old), str(replacement.pk))


@pytest.mark.django_db
@pytest.mark.parametrize("path", ["save", "partial", "update", "bulk_update", "upsert"])
def test_direct_business_writes_are_rejected(business_case, path):
    record, _, _, changes, _ = business_case
    change(record, changes)
    name = next(iter(changes))
    with pytest.raises(ValidationError):
        if path == "save":
            record.save()
        elif path == "partial":
            record.save(update_fields=[name])
        elif path == "update":
            type(record).objects.filter(pk=record.pk).update(**changes)
        elif path == "bulk_update":
            type(record).objects.bulk_update([record], [name])
        else:
            type(record).objects.bulk_create([record], update_conflicts=True, update_fields=[name], unique_fields=["pk"])
    assert not audits(record).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("failure", [ValidationError, IntegrityError])
def test_second_audit_failure_rolls_back_data_and_first_entry(business_case, monkeypatch, failure):
    record, service, _, changes, _ = business_case
    token = record_edit_fingerprint(record)
    save = AuditLogEntry.save
    count = 0

    def fail_second(entry, *args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise failure("audit failure")
        return save(entry, *args, **kwargs)

    monkeypatch.setattr(AuditLogEntry, "save", fail_second)
    change(record, changes)
    with pytest.raises(failure, match="audit failure"):
        service(record, source_fingerprint=token, razlog="Popravek")
    record.refresh_from_db()
    assert count == 2 and record_edit_fingerprint(record) == token
    assert not audits(record).exists()


@pytest.mark.django_db(transaction=True)
def test_concurrent_edits_have_one_winner(business_case):
    record, service, _, changes, _ = business_case
    token = record_edit_fingerprint(record)
    gate = Barrier(2)

    def writer(_):
        try:
            candidate = type(record).objects.get(pk=record.pk)
            change(candidate, changes)
            gate.wait(timeout=10)
            try:
                service(candidate, source_fingerprint=token, razlog="Sočasni popravek")
            except ValidationError:
                return "conflict"
            return "saved"
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(writer, range(2)))
    assert sorted(results) == ["conflict", "saved"]
    assert audits(record).count() == len(changes)


@pytest.mark.django_db
def test_ui_edits_require_reason_and_save_audits(business_case, client):
    record, _, _, changes, url = business_case
    page = client.get(url)
    assert 'name="edit_fingerprint"' in page.content.decode()
    assert client.post(url, form_data(business_case, reason="", **changes)).status_code == 200
    assert not audits(record).exists()
    assert client.post(url, form_data(business_case, **changes)).status_code == 302
    assert audits(record).count() == len(changes)


@pytest.mark.django_db
def test_ui_save_conflict_is_inline(business_case, client, monkeypatch):
    record, _, form_class, changes, url = business_case
    original_save = form_class.save

    def intervening_save(form, *args, **kwargs):
        fresh = type(record).objects.get(pk=record.pk)
        fresh._business_edit = (record_edit_fingerprint(fresh), "Drug popravek")
        change(fresh, changes)
        fresh.save()
        return original_save(form, *args, **kwargs)

    monkeypatch.setattr(form_class, "save", intervening_save)
    response = client.post(url, form_data(business_case, **changes))
    assert response.status_code == 200
    assert "Osvežite" in response.content.decode()
    assert audits(record).count() == len(changes)


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["recipient", "service", "tenant", "property", "unit"])
def test_state_transitions_audit_once_and_keep_stale_edit_invalid(kind):
    lease = make_hierarchy()
    if kind == "recipient":
        record = RecipientContact.objects.create(lease=lease, email="to@example.com", vloga="TO", kanal="PDF")
        off, on = deaktiviraj_prejemnika, ponovno_aktiviraj_prejemnika
        field = "aktiven"
    elif kind == "service":
        record = ServiceCatalogItem.objects.create(naziv="Storitev", enota_mere="kos")
        off = lambda record: deaktiviraj_storitev(record.pk)
        on = lambda record: ponovno_aktiviraj_storitev(record.pk)
        field = "aktivna"
    else:
        record = {"tenant": lease.tenant, "property": lease.unit.property, "unit": lease.unit}[kind]
        off, on = arhiviraj_poslovni_zapis, ponovno_aktiviraj_poslovni_zapis
        field = "aktiven" if kind == "tenant" else "aktivna"
    token = record_edit_fingerprint(record)
    assert off(record).uspesno
    off(record)
    assert audits(record).count() == 1
    assert on(record).uspesno
    on(record)
    entries = list(audits(record))
    assert [(entry.polje, entry.stara_vrednost, entry.nova_vrednost) for entry in entries] == [
        (field, "True", "False"), (field, "False", "True"),
    ]
    record.refresh_from_db()
    assert record_edit_fingerprint(record) != token


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["recipient", "service"])
@pytest.mark.parametrize("active", [False, True])
def test_state_audit_failure_rolls_back(kind, active, monkeypatch):
    lease = make_hierarchy()
    if kind == "recipient":
        record = RecipientContact.objects.create(lease=lease, email="to@example.com", vloga="TO", kanal="PDF", aktiven=active)
        service = deaktiviraj_prejemnika if active else ponovno_aktiviraj_prejemnika
        field = "aktiven"
    else:
        record = ServiceCatalogItem.objects.create(naziv="Storitev", enota_mere="kos", aktivna=active)
        service = lambda record: (deaktiviraj_storitev if active else ponovno_aktiviraj_storitev)(record.pk)
        field = "aktivna"

    def fail(*args, **kwargs):
        raise IntegrityError("audit failure")

    monkeypatch.setattr(AuditLogEntry, "save", fail)
    with pytest.raises(IntegrityError):
        service(record)
    record.refresh_from_db()
    assert getattr(record, field) == active
    assert not audits(record).exists()


@pytest.mark.django_db
def test_signature_replacement_audits_path_preserves_old_asset_and_resets_checksum(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    old = tmp_path / "old.png"
    old.write_bytes(slika_bytes())
    record = Signatory.objects.create(company=make_hierarchy().unit.property.company, ime_priimek="Ana", podpis_slika="old.png", podpis_checksum="a" * 64, velja_od=date(2020, 1, 1))
    token = record_edit_fingerprint(record)
    record.podpis_slika = SimpleUploadedFile("old.png", slika_bytes())
    with pytest.raises(ValidationError):
        record.save()
    with pytest.raises(ValidationError, match="Razlog"):
        save_signatory_edit(record, source_fingerprint=token)
    save_signatory_edit(record, source_fingerprint=token, razlog="Nov podpis")
    assert record.podpis_checksum == ""
    assert old.read_bytes() == slika_bytes()
    entry = audits(record).get()
    assert (entry.polje, entry.stara_vrednost, entry.nova_vrednost) == ("podpis_slika", "old.png", record.podpis_slika.name)
    assert record.podpis_slika.name.startswith("slike/")


@pytest.mark.django_db
def test_signature_audit_failure_removes_only_new_asset_and_allows_retry(settings, tmp_path, monkeypatch):
    settings.MEDIA_ROOT = tmp_path
    old = tmp_path / "old.png"
    old.write_bytes(slika_bytes())
    record = Signatory.objects.create(company=make_hierarchy().unit.property.company, ime_priimek="Ana", podpis_slika="old.png", podpis_checksum="a" * 64, velja_od=date(2020, 1, 1))
    token = record_edit_fingerprint(record)
    record.podpis_slika = SimpleUploadedFile("replacement.png", slika_bytes())
    record._business_edit = (token, "Nov podpis")
    original_save = AuditLogEntry.save

    def fail(*args, **kwargs):
        raise IntegrityError("audit failure")

    monkeypatch.setattr(AuditLogEntry, "save", fail)
    with pytest.raises(IntegrityError):
        record.save()
    stored = Signatory.objects.get(pk=record.pk)
    assert stored.podpis_slika.name == "old.png" and stored.podpis_checksum == "a" * 64
    assert [path for path in tmp_path.rglob("*") if path.is_file()] == [old]
    assert not audits(record).exists()
    monkeypatch.setattr(AuditLogEntry, "save", original_save)
    record.save()
    assert audits(record).count() == 1
    assert old.read_bytes() == slika_bytes()


@pytest.mark.django_db
def test_lifecycle_fields_cannot_be_smuggled_through_edit_service(business_case):
    record, service, _, changes, _ = business_case
    token = record_edit_fingerprint(record)
    change(record, changes)
    field = next(field for field in ("aktiven", "aktivna", "podpis_checksum") if hasattr(record, field))
    setattr(record, field, "forged" if field == "podpis_checksum" else False)
    with pytest.raises(ValidationError, match="namensko"):
        service(record, source_fingerprint=token, razlog="Popravek")
    record.refresh_from_db()
    assert record_edit_fingerprint(record) == token
    assert not audits(record).exists()
