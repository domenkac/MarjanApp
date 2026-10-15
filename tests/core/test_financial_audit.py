from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from threading import Barrier

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connections, transaction
from django.forms.models import model_to_dict

from catalogs.models import VatRegime
from catalogs.services import save_vat_regime_edit
from companies.models import Company, NumberSequence
from companies.services import (
    potrdi_ddv_nastavitve,
    save_company_edit,
    veljavna_potrditev_ddv,
    veljavna_potrditev_vzorca,
)
from contracts.models import Lease, LeaseItem
from contracts.renewals import save_lease_edit
from contracts.services import rocno_popravi_postavko, save_lease_item_edit
from core.models import AuditLogEntry, AuditLogQuerySet
from core.services import record_edit_fingerprint
from invoicing.models import Invoice, InvoiceLine
from invoicing.services import potrdi_osnutek_np
from tests.financial_fixtures import save_financial_fixture, update_financial_fixture
from tests.invoicing import test_priprava_np
from tests.invoicing.test_priprava_np import dodaj_postavko, pripravi
from tests.setup_helpers import approve_sample, prepare_initial_setup
from tests.ui.test_contract_leases import make_hierarchy, make_vat
from ui.forms import CompanyForm, LeaseForm, LeaseItemDetailsForm, VatRegimeForm

pogodba = test_priprava_np.pogodba


@pytest.fixture(params=["company", "lease", "item", "vat"])
def edit_case(request):
    lease = make_hierarchy()
    vat = make_vat()
    vat.eslog_koda = "S"
    save_financial_fixture(vat)
    item = LeaseItem.objects.create(lease=lease, vat_regime=vat, znesek_neto_mesecno=Decimal("100.00"))
    return {
        "company": (lease.unit.property.company, save_company_edit, CompanyForm,
                    {"naziv": "Novo podjetje", "naslov": "Nov naslov"}, {"trr": "invalid"}),
        "lease": (lease, save_lease_edit, LeaseForm,
                  {"oznaka_pogodbe": "P-2", "rok_placila_dni": 21}, {"tip_trajanja": "invalid"}),
        "item": (item, save_lease_item_edit, LeaseItemDetailsForm,
                 {"naziv_postavke": "Parkiranje", "referenca_listine": "Aneks 2"}, {"indeks_ob_osnovi": "invalid"}),
        "vat": (vat, save_vat_regime_edit, VatRegimeForm,
                {"stopnja": Decimal("9.50"), "klavzula": "Nova klavzula"}, {"stopnja": "invalid"}),
    }[request.param]


def audits(record):
    return AuditLogEntry.objects.filter(
        content_type=ContentType.objects.get_for_model(record), object_id=record.pk,
    ).order_by("pk")


def change(record, values):
    for name, value in values.items():
        setattr(record, name, value)


def form_data(record, form_class, *, reason="Aneks", **changes):
    data = model_to_dict(record, fields=form_class._meta.fields)
    data.pop("logotip", None)
    data.update(edit_fingerprint=record_edit_fingerprint(record), razlog=reason)
    data.update(changes)
    return data


@pytest.mark.django_db
def test_multi_field_edits_have_exact_values_and_identity(edit_case):
    record, service, _, changes, _ = edit_case
    token = record_edit_fingerprint(record)
    old = {name: str(getattr(record, name)) for name in changes}
    change(record, changes)
    service(record, source_fingerprint=token, razlog="  Aneks 2  ")
    entries = list(audits(record))
    assert len(entries) == len(changes)
    assert {entry.polje: (entry.stara_vrednost, entry.nova_vrednost) for entry in entries} == {
        name: (old[name], str(getattr(record, name))) for name in changes
    }
    assert all(entry.razlog == "Aneks 2" and entry.created_at for entry in entries)
    assert all(entry.content_type.model_class() is type(record) and entry.object_id == record.pk for entry in entries)


@pytest.mark.django_db
@pytest.mark.parametrize("reason", [None, "", "   "])
def test_missing_reason_rejects_entire_edit(edit_case, reason):
    record, service, _, changes, _ = edit_case
    token = record_edit_fingerprint(record)
    change(record, changes)
    with pytest.raises(ValidationError, match="Razlog"):
        service(record, source_fingerprint=token, razlog=reason)
    record.refresh_from_db()
    assert record_edit_fingerprint(record) == token
    assert not audits(record).exists()


@pytest.mark.django_db
def test_noop_needs_no_reason_and_writes_no_audit(edit_case):
    record, service, form_class, _, _ = edit_case
    service(record, source_fingerprint=record_edit_fingerprint(record))
    form = form_class(form_data(record, form_class, reason=""), instance=record)
    assert form.is_valid(), form.errors
    form.save()
    assert not audits(record).exists()


@pytest.mark.django_db
def test_invalid_edit_rolls_back_every_field(edit_case):
    record, service, _, changes, invalid = edit_case
    token = record_edit_fingerprint(record)
    change(record, changes | invalid)
    with pytest.raises(ValidationError):
        service(record, source_fingerprint=token, razlog="Aneks")
    record.refresh_from_db()
    assert record_edit_fingerprint(record) == token


@pytest.mark.django_db
@pytest.mark.parametrize("failure", [ValidationError, IntegrityError])
def test_second_audit_failure_rolls_back_business_change_and_first_audit(edit_case, monkeypatch, failure):
    record, service, _, changes, _ = edit_case
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
        service(record, source_fingerprint=token, razlog="Aneks")
    record.refresh_from_db()
    assert count == 2 and record_edit_fingerprint(record) == token
    assert not audits(record).exists()


@pytest.mark.django_db
def test_forms_require_reason_only_for_financial_changes(edit_case):
    record, _, form_class, changes, _ = edit_case
    form = form_class(form_data(record, form_class, reason=" ", **changes), instance=record)
    assert not form.is_valid() and "razlog" in form.errors
    assert not audits(record).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("token", ["", "forged"])
def test_missing_or_invalid_source_is_rejected(edit_case, token):
    record, service, form_class, changes, _ = edit_case
    data = form_data(record, form_class, **changes)
    data["edit_fingerprint"] = token
    assert not form_class(data, instance=record).is_valid()
    change(record, changes)
    with pytest.raises(ValidationError):
        service(record, source_fingerprint=token, razlog="Aneks")
    assert not audits(record).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("deferred", [False, True])
def test_save_rechecks_source_after_form_validation(edit_case, deferred):
    record, service, form_class, changes, _ = edit_case
    data = form_data(record, form_class, **changes)
    form = form_class(data, instance=record)
    assert form.is_valid(), form.errors
    fresh = type(record).objects.get(pk=record.pk)
    token = record_edit_fingerprint(fresh)
    change(fresh, changes)
    service(fresh, source_fingerprint=token, razlog="Other edit")
    assert not form_class(data, instance=type(record).objects.get(pk=record.pk)).is_valid()
    with pytest.raises(ValidationError, match="Osvežite"):
        form.save(commit=False).save() if deferred else form.save()
    assert audits(record).count() == len(changes)


@pytest.mark.django_db
def test_deferred_save_audits_and_cannot_retry_without_safeguards(edit_case, monkeypatch):
    record, _, form_class, changes, _ = edit_case
    form = form_class(form_data(record, form_class, **changes), instance=record)
    assert form.is_valid(), form.errors
    deferred = form.save(commit=False)
    original_save = AuditLogEntry.save

    def fail(*args, **kwargs):
        raise IntegrityError("audit failure")

    monkeypatch.setattr(AuditLogEntry, "save", fail)
    for _ in range(2):
        with pytest.raises(IntegrityError):
            deferred.save()
        assert not audits(record).exists()
    monkeypatch.setattr(AuditLogEntry, "save", original_save)
    deferred.save()
    assert audits(record).count() == len(changes)


@pytest.mark.django_db(transaction=True)
def test_concurrent_edits_have_one_winner_and_one_conflict(edit_case):
    record, service, _, changes, _ = edit_case
    token = record_edit_fingerprint(record)
    gate = Barrier(2)

    def writer():
        try:
            candidate = type(record).objects.get(pk=record.pk)
            change(candidate, changes)
            gate.wait(timeout=10)
            try:
                service(candidate, source_fingerprint=token, razlog="Concurrent edit")
            except ValidationError:
                return "conflict"
            return "saved"
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: writer(), range(2)))
    assert sorted(results) == ["conflict", "saved"]
    assert audits(record).count() == len(changes)


@pytest.mark.django_db
def test_contact_and_keychain_changes_never_enter_financial_audit():
    company = make_hierarchy().unit.property.company
    form = CompanyForm(form_data(
        company, CompanyForm, reason="", email_posiljatelj="new@example.com",
        keychain_kljuc_posiljatelja="secret-reference",
    ), instance=company)
    assert form.is_valid(), form.errors
    form.save()
    company.refresh_from_db()
    assert company.email_posiljatelj == "new@example.com"
    assert not audits(company).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("path", ["clean", "save", "update", "bulk_update", "upsert", "service", "form"])
def test_vat_method_is_immutable_on_supported_paths(path):
    vat = make_vat()
    token = record_edit_fingerprint(vat)
    vat.nacin_obracuna = VatRegime.NacinObracuna.NI_PREDMET
    with pytest.raises(ValidationError):
        if path == "clean":
            vat.full_clean()
        elif path == "save":
            vat.save()
        elif path == "update":
            VatRegime.objects.filter(pk=vat.pk).update(nacin_obracuna=vat.nacin_obracuna)
        elif path == "bulk_update":
            VatRegime.objects.bulk_update([vat], ["nacin_obracuna"])
        elif path == "upsert":
            VatRegime.objects.bulk_create([vat], update_conflicts=True, update_fields=["nacin_obracuna"], unique_fields=["sifra"])
        elif path == "service":
            save_vat_regime_edit(vat, source_fingerprint=token, razlog="New method")
        else:
            vat.refresh_from_db()
            form = VatRegimeForm(form_data(vat, VatRegimeForm, nacin_obracuna="NI_PREDMET"), instance=vat)
            assert not form.is_valid() and "nacin_obracuna" in form.errors
            raise ValidationError(form.errors.as_data())
    vat.refresh_from_db()
    assert vat.nacin_obracuna == "OBDAVCENO" and not audits(vat).exists()
    new = make_vat("NEW")
    assert new.pk != vat.pk


@pytest.mark.django_db
def test_correction_stale_source_preserves_specialized_audit():
    lease = make_hierarchy()
    item = LeaseItem.objects.create(lease=lease, vat_regime=make_vat(), znesek_neto_mesecno=Decimal("100.00"))
    token = record_edit_fingerprint(item)
    rocno_popravi_postavko(item, source_fingerprint=token, razlog="Aneks", znesek_neto_mesecno=Decimal("125.00"))
    assert audits(item).count() == 1
    entry = audits(item).get()
    assert (entry.polje, entry.stara_vrednost, entry.nova_vrednost) == ("znesek_neto_mesecno", "100.00", "125.00")
    with pytest.raises(ValidationError):
        rocno_popravi_postavko(item, source_fingerprint=token, razlog="Stale", datum_zacetka_postavke=date(2026, 2, 1))
    item.refresh_from_db()
    assert item.datum_zacetka_postavke is None and audits(item).count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize("path", ["save", "delete", "update", "query_delete", "bulk_update", "upsert"])
def test_audit_history_is_append_only(path):
    company = make_hierarchy().unit.property.company
    entry = AuditLogEntry.objects.create(content_type=ContentType.objects.get_for_model(Company), object_id=company.pk, razlog="Original")
    entry.razlog = "Replacement"
    with pytest.raises(ValidationError):
        if path == "save":
            entry.save()
        elif path == "delete":
            entry.delete()
        elif path == "update":
            audits(company).update(razlog="Replacement")
        elif path == "query_delete":
            audits(company).delete()
        elif path == "bulk_update":
            AuditLogEntry.objects.bulk_update([entry], ["razlog"])
        else:
            AuditLogEntry.objects.bulk_create([entry], update_conflicts=True, update_fields=["razlog"], unique_fields=["id"])
    entry.refresh_from_db()
    assert entry.razlog == "Original"


@pytest.mark.django_db
@pytest.mark.parametrize("failure", [IntegrityError, ValidationError])
def test_ui_audit_failure_returns_form_and_rolls_back(edit_case, client, monkeypatch, failure):
    record, _, form_class, changes, _ = edit_case
    data = form_data(record, form_class, **changes)
    data = {name: "" if value is None else value for name, value in data.items()}
    url = {
        Company: f"/nastavitve/podjetja/{record.pk}/",
        Lease: f"/pogodbe/{record.pk}/",
        LeaseItem: f"/pogodbe/postavke/{record.pk}/",
        VatRegime: f"/nastavitve/ddv/{record.pk}/",
    }[type(record)]
    token = record_edit_fingerprint(record)

    def fail(*args, **kwargs):
        raise failure("audit failure")

    monkeypatch.setattr(AuditLogEntry, "save", fail)
    response = client.post(url, data)
    assert response.status_code == 200 and response.context["form"].errors
    record.refresh_from_db()
    assert record_edit_fingerprint(record) == token and not audits(record).exists()


@pytest.mark.django_db
def test_correction_noop_missing_source_and_stale_form(client):
    item = LeaseItem.objects.create(lease=make_hierarchy(), vat_regime=make_vat(), znesek_neto_mesecno=Decimal(100))
    item.refresh_from_db()
    url = f"/pogodbe/postavke/{item.pk}/popravek/"
    data = {
        "edit_fingerprint": client.get(url).context["form"].initial["edit_fingerprint"],
        "znesek_neto_mesecno": "100.0", "datum_zacetka_postavke": "",
        "datum_prenehanja_postavke": "", "razlog": "",
    }
    assert client.post(url, data).status_code == 302
    assert not audits(item).exists()
    missing = data | {"edit_fingerprint": "", "znesek_neto_mesecno": "125", "razlog": "Aneks"}
    response = client.post(url, missing)
    assert response.status_code == 200 and "Osvežite" in str(response.context["form"].errors)
    data.update(znesek_neto_mesecno="125", razlog="Aneks")
    assert client.post(url, data).status_code == 302
    response = client.post(url, data | {"znesek_neto_mesecno": "150"})
    assert response.status_code == 200 and "Osvežite" in str(response.context["form"].errors)
    item.refresh_from_db()
    assert item.znesek_neto_mesecno == Decimal("125.00") and audits(item).count() == 1


@pytest.mark.django_db
def test_decimal_scale_is_not_a_stale_edit():
    item = LeaseItem.objects.create(lease=make_hierarchy(), vat_regime=make_vat(), znesek_neto_mesecno=Decimal(100))
    token = record_edit_fingerprint(item)
    item.refresh_from_db()
    assert record_edit_fingerprint(item) == token


@pytest.mark.django_db
def test_item_save_reloads_vat_validation_dependencies():
    item = LeaseItem.objects.create(lease=make_hierarchy(), vat_regime=make_vat(), znesek_neto_mesecno=Decimal(100))
    data = form_data(item, LeaseItemDetailsForm, naziv_postavke="Changed")
    form = LeaseItemDetailsForm(data, instance=item)
    assert form.is_valid(), form.errors
    update_financial_fixture(VatRegime.objects.filter(pk=item.vat_regime_id), zahteva_polni_odbitek=True)
    with pytest.raises(ValidationError):
        form.save()
    item.refresh_from_db()
    assert item.naziv_postavke == "Najemnina" and not audits(item).exists()


def test_audited_edits_preserve_numbered_invoice_and_frozen_snapshots(pogodba):
    item = dodaj_postavko(pogodba)
    company = pogodba.unit.property.company
    prepare_initial_setup(company, pogodba)
    invoice = potrdi_osnutek_np(pripravi(pogodba).invoice)
    company.refresh_from_db()
    assert invoice.stevilka
    frozen = Invoice.objects.filter(pk=invoice.pk).values().get()
    frozen_lines = list(InvoiceLine.objects.filter(invoice=invoice).values())
    frozen_sequences = list(NumberSequence.objects.filter(company=company).values())
    for record, service, changes in (
        (company, save_company_edit, {"naziv": "New company", "naslov": "New address"}),
        (pogodba, save_lease_edit, {"rok_placila_dni": 21}),
        (item, save_lease_item_edit, {"referenca_listine": "Aneks 2"}),
        (item.vat_regime, save_vat_regime_edit, {"stopnja": Decimal("9.50"), "klavzula": "New clause"}),
    ):
        source = record_edit_fingerprint(record)
        change(record, changes)
        service(record, source_fingerprint=source, razlog="Aneks")
    rocno_popravi_postavko(item, source_fingerprint=record_edit_fingerprint(item), razlog="Aneks", znesek_neto_mesecno=Decimal("125.00"))
    assert Invoice.objects.filter(pk=invoice.pk).values().get() == frozen
    assert list(InvoiceLine.objects.filter(invoice=invoice).values()) == frozen_lines
    assert list(NumberSequence.objects.filter(company=company).values()) == frozen_sequences


@pytest.mark.parametrize("target", ["company", "lease", "item", "vat"])
def test_audited_edits_preserve_vat_and_sample_staleness_rules(pogodba, target):
    item = dodaj_postavko(pogodba)
    company = pogodba.unit.property.company
    prepare_initial_setup(company, pogodba)
    company.refresh_from_db()
    assert veljavna_potrditev_ddv(company).stanje == "valid"
    assert veljavna_potrditev_vzorca(company).stanje == "valid"
    record, service, changes = {
        "company": (company, save_company_edit, {"naziv": "Changed name"}),
        "lease": (pogodba, save_lease_edit, {"rok_placila_dni": 21}),
        "item": (item, save_lease_item_edit, {"naziv_postavke": "Changed item"}),
        "vat": (item.vat_regime, save_vat_regime_edit, {"stopnja": Decimal("9.50")}),
    }[target]
    source = record_edit_fingerprint(record)
    change(record, changes)
    service(record, source_fingerprint=source, razlog="Aneks")
    assert veljavna_potrditev_vzorca(company).stanje == "stale"
    assert veljavna_potrditev_ddv(company).stanje == ("stale" if target == "vat" else "valid")
    potrdi_ddv_nastavitve(company)
    approve_sample(company, pogodba)
    assert veljavna_potrditev_ddv(company).stanje == veljavna_potrditev_vzorca(company).stanje == "valid"


@pytest.mark.django_db
def test_reverting_values_does_not_revalidate_an_old_form(edit_case):
    record, service, _, changes, _ = edit_case
    source = record_edit_fingerprint(record)
    original = {name: getattr(record, name) for name in changes}
    change(record, changes)
    service(record, source_fingerprint=source, razlog="First edit")
    current = record_edit_fingerprint(record)
    change(record, original)
    service(record, source_fingerprint=current, razlog="Revert")
    with pytest.raises(ValidationError):
        service(record, source_fingerprint=source, razlog="Old form")
    assert audits(record).count() == 2 * len(changes)


@pytest.mark.django_db(transaction=True)
def test_correction_concurrency_rejects_losing_edit():
    item = LeaseItem.objects.create(lease=make_hierarchy(), vat_regime=make_vat(), znesek_neto_mesecno=Decimal("100.00"))
    source = record_edit_fingerprint(item)
    gate = Barrier(2)

    def writer(amount):
        try:
            gate.wait(timeout=10)
            try:
                rocno_popravi_postavko(item, source_fingerprint=source, razlog="Aneks", znesek_neto_mesecno=amount)
            except ValidationError:
                return "conflict"
            return "saved"
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(writer, [Decimal("125.00"), Decimal("150.00")]))
    assert sorted(results) == ["conflict", "saved"]
    item.refresh_from_db()
    entry = audits(item).get()
    assert entry.stara_vrednost == "100.00" and entry.nova_vrednost == str(item.znesek_neto_mesecno)


@pytest.mark.django_db
@pytest.mark.parametrize("model", [Company, Lease, LeaseItem])
def test_ordinary_edit_cannot_bypass_specialized_services(model):
    lease = make_hierarchy()
    item = LeaseItem.objects.create(lease=lease, vat_regime=make_vat(), znesek_neto_mesecno=Decimal("100.00"))
    record, service, field = {
        Company: (lease.unit.property.company, save_company_edit, "aktivno"),
        Lease: (lease, save_lease_edit, "arhivirana"),
        LeaseItem: (item, save_lease_item_edit, "znesek_neto_mesecno"),
    }[model]
    source = record_edit_fingerprint(record)
    setattr(record, field, Decimal("125.00") if model is LeaseItem else not getattr(record, field))
    with pytest.raises(ValidationError):
        service(record, source_fingerprint=source, razlog="Bypass")
    record.refresh_from_db()
    assert record_edit_fingerprint(record) == source


@pytest.mark.django_db
def test_audit_save_never_overwrites_a_row_after_an_insert_race(monkeypatch):
    company = make_hierarchy().unit.property.company
    entry = AuditLogEntry.objects.create(
        content_type=ContentType.objects.get_for_model(company), object_id=company.pk,
        razlog="Original",
    )
    entry.razlog = "Replacement"
    monkeypatch.setattr(AuditLogQuerySet, "exists", lambda self: False)
    with pytest.raises(IntegrityError), transaction.atomic():
        entry.save()
    entry.refresh_from_db()
    assert entry.razlog == "Original"
