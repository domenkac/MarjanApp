"""Consequential configuration edits use synthetic data and isolated assets."""

import json
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from threading import Barrier

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, connections, transaction
from django.forms.models import model_to_dict

from archive.configuration import (
    ArchiveConfigurationError,
    verify_and_save_archive_roots,
)
from companies.models import Company
from companies.services import save_company_edit
from core.models import AuditLogEntry
from core.services import record_edit_fingerprint
from delivery.configuration import save_monthly_summary_configuration
from delivery.models import MonthlySummaryConfiguration, SmtpVerification
from delivery.verification import verification_status
from tests.image_data import slika_bytes
from tests.smtp_helpers import approve_smtp
from tests.ui.test_contract_leases import make_company
from ui.forms import CompanyForm, MonthlySummaryConfigurationForm

pytestmark = pytest.mark.django_db(transaction=True)


def audits(record):
    return AuditLogEntry.objects.filter(
        content_type=ContentType.objects.get_for_model(record), object_id=record.pk,
    ).order_by("pk")


def roots(tmp_path, prefix=""):
    paths = [tmp_path / (prefix + name) for name in ("local", "icloud", "dropbox")]
    for path in paths:
        path.mkdir()
    return paths


@pytest.mark.parametrize("field,value", [
    ("email_racunovodja", "accountant-new@example.com"),
    ("email_posiljatelj", "sender-new@example.com"),
    ("keychain_kljuc_posiljatelja", "private-new-reference"),
])
def test_company_consequential_edit_is_audited(field, value):
    company = make_company()
    token = record_edit_fingerprint(company)
    setattr(company, field, value)
    save_company_edit(company, source_fingerprint=token, razlog="Menjava nastavitev")
    entry = audits(company).get()
    assert entry.polje == field
    assert entry.razlog == "Menjava nastavitev" and entry.created_at
    if "keychain" in field:
        assert value not in entry.nova_vrednost
        assert entry.stara_vrednost != entry.nova_vrednost
    else:
        assert entry.nova_vrednost == value


def test_summary_ui_edit_is_audited(client):
    configuration = MonthlySummaryConfiguration.objects.create(
        email="owner@example.com", keychain_credential_key="private-old-reference",
    )
    response = client.post("/nastavitve/zbirno-porocilo/", {
        "email": "new-owner@example.com", "keychain_credential_key": "private-new-reference",
        "razlog": "Menjava predala", "edit_fingerprint": record_edit_fingerprint(configuration),
    })
    assert response.status_code == 302
    assert set(audits(configuration).values_list("polje", flat=True)) == {
        "email", "keychain_credential_key",
    }


def test_archive_ui_edit_is_audited(client, tmp_path):
    configuration = verify_and_save_archive_roots(*roots(tmp_path, "old-"))
    response = client.post("/nastavitve/arhiv/", {
        **dict(zip(("local_root", "icloud_root", "dropbox_root"), roots(tmp_path, "new-"))),
        "razlog": "Menjava lokacij", "edit_fingerprint": record_edit_fingerprint(configuration),
    })
    assert response.status_code in (200, 302)
    assert set(audits(configuration).values_list("polje", flat=True)) == {
        "local_root", "icloud_root", "dropbox_root",
    }


def save_archive(configuration, **kwargs):
    return verify_and_save_archive_roots(
        configuration.local_root, configuration.icloud_root, configuration.dropbox_root, **kwargs,
    )


@pytest.fixture(params=["company", "summary", "archive"])
def edit_case(request, tmp_path):
    if request.param == "company":
        return make_company(), save_company_edit, {
            "email_racunovodja": "new-accountant@example.com",
            "email_posiljatelj": "new-sender@example.com",
        }
    if request.param == "summary":
        return MonthlySummaryConfiguration.objects.create(
            email="owner@example.com", keychain_credential_key="private-old-reference",
        ), save_monthly_summary_configuration, {
            "email": "new-owner@example.com", "keychain_credential_key": "private-new-reference",
        }
    return verify_and_save_archive_roots(*roots(tmp_path, "old-")), save_archive, dict(zip(
        ("local_root", "icloud_root", "dropbox_root"), map(str, roots(tmp_path, "new-")),
    ))


def change(record, values):
    for name, value in values.items():
        setattr(record, name, value)


def test_domain_noop_needs_no_reason_and_has_no_change_audit(edit_case):
    record, service, _ = edit_case
    service(record, source_fingerprint=record_edit_fingerprint(record))
    assert not audits(record).exists()


@pytest.mark.parametrize("reason", [None, "", " \t\n"])
def test_domain_requires_reason_and_preserves_old_configuration(edit_case, reason):
    record, service, changes = edit_case
    token = record_edit_fingerprint(record)
    change(record, changes)
    with pytest.raises(ValidationError, match="Razlog"):
        service(record, source_fingerprint=token, razlog=reason)
    record.refresh_from_db()
    assert record_edit_fingerprint(record) == token and not audits(record).exists()


def test_domain_multiple_fields_have_exact_identity_values_and_time(edit_case):
    record, service, changes = edit_case
    old = {name: str(getattr(record, name)) for name in changes}
    token = record_edit_fingerprint(record)
    change(record, changes)
    service(record, source_fingerprint=token, razlog="  Menjava nastavitev  ")
    entries = list(audits(record))
    assert len(entries) == len(changes)
    for entry in entries:
        assert entry.object_id == record.pk and entry.content_type.model_class() is type(record)
        assert entry.razlog == "Menjava nastavitev" and entry.created_at
        if "keychain" in entry.polje:
            assert entry.stara_vrednost.startswith("hmac-sha256:")
            assert entry.nova_vrednost.startswith("hmac-sha256:")
            assert old[entry.polje] not in entry.stara_vrednost
            assert changes[entry.polje] not in entry.nova_vrednost
            assert entry.stara_vrednost != entry.nova_vrednost
        else:
            assert (entry.stara_vrednost, entry.nova_vrednost) == (old[entry.polje], changes[entry.polje])


@pytest.mark.parametrize("token", ["", "forged"])
def test_domain_rejects_missing_or_forged_fingerprint(edit_case, token):
    record, service, changes = edit_case
    original = record_edit_fingerprint(record)
    change(record, changes)
    with pytest.raises(ValidationError, match="Osvežite"):
        service(record, source_fingerprint=token, razlog="Menjava")
    record.refresh_from_db()
    assert record_edit_fingerprint(record) == original and not audits(record).exists()


def test_domain_rejects_stale_competing_snapshot(edit_case):
    record, service, changes = edit_case
    token = record_edit_fingerprint(record)
    competing = type(record).objects.get(pk=record.pk)
    change(competing, changes)
    service(competing, source_fingerprint=token, razlog="Prva sprememba")
    with pytest.raises(ValidationError, match="Osvežite"):
        service(record, source_fingerprint=token, razlog="Zastarela sprememba")
    assert audits(record).count() == len(changes)


@pytest.mark.parametrize("failure", [IntegrityError, ValidationError])
def test_second_audit_failure_rolls_back_all_fields_and_first_audit(edit_case, monkeypatch, failure):
    record, service, changes = edit_case
    token = record_edit_fingerprint(record)
    original_save = AuditLogEntry.save
    calls = []

    def fail_second(entry, *args, **kwargs):
        assert connection.in_atomic_block
        calls.append(entry.polje)
        if len(calls) == 2:
            raise failure("synthetic audit failure")
        return original_save(entry, *args, **kwargs)

    monkeypatch.setattr(AuditLogEntry, "save", fail_second)
    change(record, changes)
    with pytest.raises(failure, match="synthetic audit failure"):
        service(record, source_fingerprint=token, razlog="Menjava")
    record.refresh_from_db()
    assert len(calls) == 2
    assert record_edit_fingerprint(record) == token and not audits(record).exists()


def test_actual_concurrent_edits_have_one_winner(edit_case):
    record, service, changes = edit_case
    token = record_edit_fingerprint(record)
    gate = Barrier(2)

    def writer(_):
        try:
            candidate = type(record).objects.get(pk=record.pk)
            change(candidate, changes)
            gate.wait(timeout=10)
            try:
                service(candidate, source_fingerprint=token, razlog="Sočasna sprememba")
            except ValidationError:
                return "conflict"
            return "saved"
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(writer, range(2))) == ["conflict", "saved"]
    assert audits(record).count() == len(changes)


@pytest.mark.parametrize("field", ["local_root", "icloud_root", "dropbox_root"])
def test_single_archive_root_change_has_only_one_audit(tmp_path, field):
    configuration = verify_and_save_archive_roots(*roots(tmp_path))
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    token = record_edit_fingerprint(configuration)
    old = getattr(configuration, field)
    setattr(configuration, field, str(replacement))
    save_archive(configuration, source_fingerprint=token, razlog="Nova lokacija")
    entry = audits(configuration).get()
    assert (entry.polje, entry.stara_vrednost, entry.nova_vrednost) == (field, old, str(replacement))


def test_archive_probes_stay_outside_transaction_and_competing_probe_edit_is_rejected(tmp_path, monkeypatch):
    configuration = verify_and_save_archive_roots(*roots(tmp_path, "old-"))
    new = roots(tmp_path, "new-")
    competitor = roots(tmp_path, "competitor-")
    from archive.configuration import _verify_root
    calls = []

    def probe(path, label):
        assert not connection.in_atomic_block and connection.get_autocommit()
        calls.append(path)
        _verify_root(path, label)
        if path == new[2]:
            verify_and_save_archive_roots(*competitor, razlog="Druga sprememba")

    monkeypatch.setattr("archive.configuration._verify_root", probe)
    with pytest.raises(ValidationError, match="Osvežite"):
        verify_and_save_archive_roots(*new, razlog="Zastarela sprememba")
    configuration.refresh_from_db()
    assert configuration.local_root == str(competitor[0])
    assert audits(configuration).count() == 3 and len(calls) == 6


@pytest.mark.parametrize("fault", ["relative", "missing", "probe"])
def test_invalid_archive_edit_preserves_configuration_and_cleans_probes(tmp_path, monkeypatch, fault):
    configuration = verify_and_save_archive_roots(*roots(tmp_path, "old-"))
    token = record_edit_fingerprint(configuration)
    new = roots(tmp_path, "new-")
    if fault == "relative":
        new[1] = "relative"
    elif fault == "missing":
        new[1].rmdir()
    else:
        def fail(*args, **kwargs):
            raise OSError("private operating system detail")
        monkeypatch.setattr("archive.configuration.os.fsync", fail)
    with pytest.raises(ArchiveConfigurationError) as error:
        verify_and_save_archive_roots(*new, razlog="Nova lokacija")
    assert "private operating system detail" not in str(error.value)
    configuration.refresh_from_db()
    assert record_edit_fingerprint(configuration) == token and not audits(configuration).exists()
    for path in tmp_path.iterdir():
        if path.is_dir():
            assert not list(path.iterdir())


@pytest.fixture(autouse=True)
def smtp_settings(settings):
    settings.MARJAN_SMTP_HOST = "smtp.example.com"
    settings.MARJAN_SMTP_PORT = 587
    settings.MARJAN_SMTP_SECURITY = "STARTTLS"
    settings.MARJAN_SMTP_TIMEOUT_SECONDS = 10
    settings.MARJAN_KEYRING_SERVICE = "synthetic-service"


@pytest.mark.parametrize("scope,field,value", [
    ("company", "email_posiljatelj", "new@example.com"),
    ("company", "keychain_kljuc_posiljatelja", "private-new-reference"),
    ("summary", "email", "new-owner@example.com"),
    ("summary", "keychain_credential_key", "private-new-reference"),
])
@pytest.mark.parametrize("audit_fails", [False, True])
def test_smtp_proof_and_audit_commit_or_rollback_together(scope, field, value, audit_fails, monkeypatch):
    company = make_company()
    summary = MonthlySummaryConfiguration.objects.create(
        email="owner@example.com", keychain_credential_key="private-old-reference",
    )
    company_proof = approve_smtp(company.pk)
    summary_proof = approve_smtp(None)
    record, service, identity, proof, other = (
        (company, save_company_edit, company.pk, company_proof, summary_proof) if scope == "company"
        else (summary, save_monthly_summary_configuration, None, summary_proof, company_proof)
    )
    token = record_edit_fingerprint(record)
    old = getattr(record, field)
    setattr(record, field, value)
    if audit_fails:
        def fail(*args, **kwargs):
            raise IntegrityError("synthetic audit failure")
        monkeypatch.setattr(AuditLogEntry, "save", fail)
        with pytest.raises(IntegrityError):
            service(record, source_fingerprint=token, razlog="Nova identiteta")
        record.refresh_from_db()
        assert getattr(record, field) == old and not audits(record).exists()
        assert verification_status(identity).verified
    else:
        service(record, source_fingerprint=token, razlog="Nova identiteta")
        assert not verification_status(identity).verified
        assert audits(record).count() == 1
        # Returning to the old identity must never restore the old proof.
        token = record_edit_fingerprint(record)
        setattr(record, field, old)
        service(record, source_fingerprint=token, razlog="Vrnitev identitete")
        assert not verification_status(identity).verified
    proof.refresh_from_db()
    other.refresh_from_db()
    assert (proof.invalidated_at is None) == audit_fails
    assert other.invalidated_at is None and SmtpVerification.objects.count() == 2


def test_company_edit_keeps_other_companies_and_summary_unchanged():
    company = make_company()
    other = make_company(davcna_stevilka="26283253", email_posiljatelj="other@example.com")
    other_before = Company.objects.filter(pk=other.pk).values().get()
    proof = approve_smtp(other.pk)
    source = record_edit_fingerprint(company)
    company.email_racunovodja = "new@example.com"
    save_company_edit(company, source_fingerprint=source, razlog="Nov računovodja")
    assert not audits(other).exists() and verification_status(other.pk).verified
    assert Company.objects.filter(pk=other.pk).values().get() == other_before
    proof.refresh_from_db()
    assert proof.invalidated_at is None


@pytest.mark.parametrize("audit_fails", [False, True])
def test_logo_change_preserves_old_asset_and_resets_checksum(settings, tmp_path, monkeypatch, audit_fails):
    settings.MEDIA_ROOT = tmp_path
    content = slika_bytes()
    old = tmp_path / "old.png"
    old.write_bytes(content)
    company = make_company(logotip="old.png", logotip_checksum=sha256(content).hexdigest())
    token = record_edit_fingerprint(company)
    company.logotip = SimpleUploadedFile("../../old.png", content, content_type="image/png")
    with pytest.raises(ValidationError, match="Razlog"):
        save_company_edit(company, source_fingerprint=token)
    original_save = AuditLogEntry.save
    if audit_fails:
        def fail(*args, **kwargs):
            raise IntegrityError("synthetic audit failure")
        monkeypatch.setattr(AuditLogEntry, "save", fail)
        with pytest.raises(IntegrityError):
            save_company_edit(company, source_fingerprint=token, razlog="Nov logotip")
        stored = Company.objects.get(pk=company.pk)
        assert stored.logotip.name == "old.png" and stored.logotip_checksum == sha256(content).hexdigest()
        assert [path for path in tmp_path.rglob("*") if path.is_file()] == [old]
        assert not audits(company).exists()
        monkeypatch.setattr(AuditLogEntry, "save", original_save)
    save_company_edit(company, source_fingerprint=token, razlog="Nov logotip")
    assert old.read_bytes() == content and company.logotip_checksum == ""
    assert company.logotip.name.startswith("slike/") and ".." not in company.logotip.name
    entry = audits(company).get()
    assert entry.polje == "logotip"
    assert json.loads(entry.stara_vrednost) == {"asset": "old.png", "sha256": sha256(content).hexdigest()}
    assert json.loads(entry.nova_vrednost) == {"asset": company.logotip.name, "sha256": sha256(content).hexdigest()}
    assert content.hex() not in entry.nova_vrednost


@pytest.mark.parametrize("form_class", [CompanyForm, MonthlySummaryConfigurationForm])
def test_configuration_form_deferred_save_rechecks_stale_source(form_class):
    record = make_company() if form_class is CompanyForm else MonthlySummaryConfiguration.objects.create(
        email="owner@example.com", keychain_credential_key="private-reference",
    )
    field = "email_posiljatelj" if form_class is CompanyForm else "email"
    data = model_to_dict(record, fields=form_class._meta.fields)
    data.pop("logotip", None)
    data.update(edit_fingerprint=record_edit_fingerprint(record), razlog="Menjava")
    data[field] = "new@example.com"
    form = form_class(data, instance=record)
    assert form.is_valid(), form.errors
    deferred = form.save(commit=False)
    competing = form_class(data, instance=type(record).objects.get(pk=record.pk))
    assert competing.is_valid(), competing.errors
    competing.save()
    with pytest.raises(ValidationError, match="Osvežite"):
        deferred.save()
    assert audits(record).count() == 1


def test_existing_validated_logo_reference_has_available_checksum_and_retains_both_assets(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    content = slika_bytes()
    for name in ("old.png", "replacement.png"):
        (tmp_path / name).write_bytes(content)
    company = make_company(logotip="old.png")
    source = record_edit_fingerprint(company)
    company.logotip = "replacement.png"
    save_company_edit(company, source_fingerprint=source, razlog="Drug obstoječi logotip")
    assert json.loads(audits(company).get().nova_vrednost) == {
        "asset": "replacement.png", "sha256": sha256(content).hexdigest(),
    }
    assert (tmp_path / "old.png").read_bytes() == (tmp_path / "replacement.png").read_bytes() == content


@pytest.mark.parametrize("workflow", ["company", "summary", "archive"])
@pytest.mark.parametrize("reason", ["", "   "])
def test_ui_requires_reason_and_get_is_read_only(client, tmp_path, workflow, reason):
    if workflow == "company":
        record = make_company()
        url = f"/nastavitve/podjetja/{record.pk}/"
        data = model_to_dict(record, fields=CompanyForm._meta.fields)
        data.pop("logotip", None)
        data["email_racunovodja"] = "new@example.com"
    elif workflow == "summary":
        record = MonthlySummaryConfiguration.objects.create(email="owner@example.com", keychain_credential_key="private-key")
        url = "/nastavitve/zbirno-porocilo/"
        data = {"email": "new@example.com", "keychain_credential_key": "private-key"}
    else:
        record = verify_and_save_archive_roots(*roots(tmp_path, "old-"))
        url = "/nastavitve/arhiv/"
        data = dict(zip(("local_root", "icloud_root", "dropbox_root"), roots(tmp_path, "new-")))
    token = record_edit_fingerprint(record)
    assert client.get(url).status_code == 200
    record.refresh_from_db()
    assert record_edit_fingerprint(record) == token and not audits(record).exists()
    response = client.post(url, data | {"razlog": reason, "edit_fingerprint": token})
    assert response.status_code == 200 and "razlog" in response.context["form"].errors
    record.refresh_from_db()
    assert record_edit_fingerprint(record) == token and not audits(record).exists()


def test_summary_creation_is_distinct_and_stale_creation_cannot_overwrite():
    first = MonthlySummaryConfiguration(email="owner@example.com", keychain_credential_key="private-key")
    save_monthly_summary_configuration(first, source_fingerprint="new")
    assert not audits(first).exists()
    competing = MonthlySummaryConfiguration(email="other@example.com", keychain_credential_key="other-key")
    with pytest.raises(ValidationError, match="Osvežite"):
        save_monthly_summary_configuration(competing, source_fingerprint="new", razlog="Tekmujoč vnos")
    assert MonthlySummaryConfiguration.objects.get().email == "owner@example.com"


def configuration_post_data(record):
    if isinstance(record, Company):
        data = model_to_dict(record, fields=CompanyForm._meta.fields)
        data.pop("logotip", None)
        return f"/nastavitve/podjetja/{record.pk}/", data
    if isinstance(record, MonthlySummaryConfiguration):
        return "/nastavitve/zbirno-porocilo/", {
            "email": record.email, "keychain_credential_key": record.keychain_credential_key,
        }
    return "/nastavitve/arhiv/", {
        name: getattr(record, name) for name in ("local_root", "icloud_root", "dropbox_root")
    }


def test_ui_noop_redirect_has_no_audit(edit_case, client):
    record, _, _ = edit_case
    url, data = configuration_post_data(record)
    data.update(edit_fingerprint=record_edit_fingerprint(record), razlog="")
    response = client.post(url, data)
    assert response.status_code == 302 and not audits(record).exists()


@pytest.mark.parametrize("failure", [IntegrityError, ValidationError])
def test_ui_audit_failure_is_inline_and_rolls_back(edit_case, client, monkeypatch, failure):
    record, _, changes = edit_case
    token = record_edit_fingerprint(record)
    url, data = configuration_post_data(record)

    def fail(*args, **kwargs):
        raise failure("Napaka revizije")

    monkeypatch.setattr(AuditLogEntry, "save", fail)
    response = client.post(url, data | changes | {"edit_fingerprint": token, "razlog": "Menjava"})
    assert response.status_code == 200 and response.context["form"].errors
    assert "private-new-reference" not in str(response.context["form"].errors)
    assert "private-old-reference" not in str(response.context["form"].errors)
    record.refresh_from_db()
    assert record_edit_fingerprint(record) == token and not audits(record).exists()


def test_ui_stale_form_is_inline_and_does_not_overwrite_winner(edit_case, client):
    record, service, changes = edit_case
    token = record_edit_fingerprint(record)
    url, data = configuration_post_data(record)
    change(record, changes)
    service(record, source_fingerprint=token, razlog="Prva sprememba")
    response = client.post(url, data | {"edit_fingerprint": token, "razlog": "Zastarela sprememba"})
    assert response.status_code == 200 and "Osvežite" in str(response.context["form"].errors)
    assert audits(record).count() == len(changes)


@pytest.mark.parametrize("url", ["/nastavitve/arhiv/", "/nastavitve/zbirno-porocilo/"])
def test_configuration_get_does_not_create_first_record_or_audit(client, url):
    from archive.models import ArchiveConfiguration

    before = AuditLogEntry.objects.count()
    assert client.get(url).status_code == 200
    assert not ArchiveConfiguration.objects.exists()
    assert not MonthlySummaryConfiguration.objects.exists()
    assert AuditLogEntry.objects.count() == before


@pytest.mark.parametrize("workflow", ["company", "summary"])
def test_outer_database_rollback_restores_identity_audit_and_smtp(workflow):
    company = make_company()
    summary = MonthlySummaryConfiguration.objects.create(email="owner@example.com", keychain_credential_key="key")
    record, service, field, identity = (
        (company, save_company_edit, "email_posiljatelj", company.pk) if workflow == "company"
        else (summary, save_monthly_summary_configuration, "email", None)
    )
    proof = approve_smtp(identity)
    token = record_edit_fingerprint(record)
    with pytest.raises(RuntimeError, match="outer rollback"), transaction.atomic():
        setattr(record, field, "new@example.com")
        service(record, source_fingerprint=token, razlog="Menjava")
        assert audits(record).count() == 1
        raise RuntimeError("outer rollback")
    record.refresh_from_db()
    proof.refresh_from_db()
    assert record_edit_fingerprint(record) == token and not audits(record).exists()
    assert proof.invalidated_at is None and verification_status(identity).verified
