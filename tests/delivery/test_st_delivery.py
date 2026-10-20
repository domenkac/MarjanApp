"""Standalone delivery uses synthetic invoices, isolated files and mocked SMTP."""
# ruff: noqa: F401, F811 -- Imported pytest fixtures.

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256
from threading import Barrier, Event
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, models, transaction

from archive.models import ArchivedFile, BackupSnapshot
from companies.models import NumberSequence
from contracts.models import RecipientContact, Tenant
from core.models import AuditLogEntry
from delivery.configuration import resolve_keyring_credential_ref
from delivery.models import DeliveryAttempt
from delivery.services import (
    build_pdf_email,
    mark_submission_started,
    prepare_attempt,
    prepare_retry_attempt,
    recover_interrupted_submissions,
    resolve_unknown_as_sent,
    resolve_unknown_user_confirmed_not_sent_and_prepare_retry,
    send_pdf_email,
)
from delivery.st_delivery import (
    cancel_st_attempt,
    prepare_st_attempt,
    review_st_delivery,
)
from tests.delivery.test_delivery_services import FakeSmtp, issued_invoice, smtp_config
from tests.rendering.test_st_pdf import confirmed, ready, setup_data
from tests.smtp_helpers import approve_smtp


@pytest.fixture
def st_invoice(issued_invoice, settings, tmp_path, monkeypatch):
    monkeypatch.setattr("smtplib.SMTP", lambda *a, **kw: pytest.fail("Real SMTP forbidden"))
    monkeypatch.setattr("smtplib.SMTP_SSL", lambda *a, **kw: pytest.fail("Real SMTP forbidden"))
    monkeypatch.setattr("keyring.get_password", lambda *a: pytest.fail("Real Keychain forbidden"))
    monkeypatch.setattr("rendering.services.render_pdf", lambda *a: pytest.fail("Implicit PDF rendering forbidden"))
    invoice = issued_invoice
    pdf = b"%PDF-1.7\nsynthetic official ST invoice\n%%EOF"
    path = f"company-{invoice.company_id}/ST-2026-0001.pdf"
    settings.RENDERING_ROOT = tmp_path / "rendered"
    artifact = settings.RENDERING_ROOT / path
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(pdf)
    models.QuerySet.update(
        type(invoice).objects.filter(pk=invoice.pk),
        document_type="ST", lease_id=None, prejemnik_id=invoice.lease.tenant_id,
        stevilka="ST-2026-0001", rendered_pdf_path=path, checksum=sha256(pdf).hexdigest(),
    )
    invoice.refresh_from_db()
    return invoice


def prepare(invoice, **changes):
    review = review_st_delivery(invoice, **({
        "recipients_to": [" Buyer@EXAMPLE.com ", "buyer@example.com"],
        "recipients_cc": ["copy@example.com"],
    } | changes))
    return prepare_st_attempt(invoice, review=review)


def send(attempt, smtp_config, fake=None):
    return send_pdf_email(
        attempt, subject="Račun ST", body="V prilogi je račun.",
        smtp_config=replace(smtp_config, username=attempt.sender),
        credential_ref=resolve_keyring_credential_ref(attempt.invoice.company), password_loader=lambda ref: "top-secret",
        smtp_factory=lambda *a: fake or FakeSmtp(),
    )


def corrupt(invoice, **changes):
    models.QuerySet.update(type(invoice).objects.filter(pk=invoice.pk), **changes)
    invoice.refresh_from_db()


def test_st_preparation_freezes_explicit_recipients_without_lease_contacts(st_invoice):
    with patch.object(RecipientContact.objects, "filter", side_effect=AssertionError("Lease contacts forbidden")):
        attempt = prepare(st_invoice)
    assert attempt.recipients_to == ["Buyer@example.com"]
    assert attempt.recipients_cc == ["copy@example.com"]
    assert attempt.recipients_bcc == ["accountant@example.com"]
    assert attempt.sender == st_invoice.company.email_posiljatelj
    assert attempt.result == "PREPARED" and attempt.submission_started_at is None
    with pytest.raises(ValidationError):
        prepare(st_invoice)
    with pytest.raises(ValidationError, match="lease"):
        prepare_attempt(st_invoice)


@pytest.mark.parametrize("changes", [
    {"status": "OSNUTEK"}, {"stevilka": None}, {"stevilka": ""},
    {"status": "POSLAN"}, {"status": "NAPAKA"}, {"status": "REZULTAT_NI_ZNAN"},
    {"document_type": "NP"}, {"document_type": "DOBROPIS"},
    {"rendered_pdf_path": "", "checksum": ""}, {"checksum": "a" * 64},
])
def test_invalid_st_cannot_prepare(st_invoice, changes):
    corrupt(st_invoice, **changes)
    with pytest.raises(ValidationError):
        prepare(st_invoice)
    assert not DeliveryAttempt.objects.exists()


@pytest.mark.parametrize("field", ["lease_id", "povezan_racun_id"])
def test_st_must_be_standalone(st_invoice, field):
    value = (st_invoice.prejemnik.lease_set.first().pk if field == "lease_id" else st_invoice.pk)
    corrupt(st_invoice, **{field: value})
    with pytest.raises(ValidationError):
        prepare(st_invoice)


@pytest.mark.parametrize("addresses", [[], [""], ["bad"], ["x@example.com\r\nBcc: secret@example.com"], ["x@example.com\n"], [None], "buyer@example.com"])
def test_invalid_or_missing_to_is_rejected(st_invoice, addresses):
    with pytest.raises(ValidationError):
        prepare(st_invoice, recipients_to=addresses)
    assert not DeliveryAttempt.objects.exists()


@pytest.mark.parametrize("changes", [
    {"recipients_cc": ["bad"]}, {"recipients_cc": ["buyer@example.com"]},
    {"recipients_to": ["accountant@example.com"]},
    {"recipients_cc": ["accountant@example.com"]},
])
def test_invalid_cc_and_cross_role_duplicates_are_rejected(st_invoice, changes):
    with pytest.raises(ValidationError):
        prepare(st_invoice, **changes)


def test_manual_recipient_never_creates_tenant(st_invoice):
    corrupt(st_invoice, prejemnik_id=None)
    before = list(Tenant.objects.values())
    with pytest.raises(ValidationError):
        prepare(st_invoice, recipients_to=[])
    assert prepare(st_invoice, recipients_to=["manual@example.com"]).recipients_to == ["manual@example.com"]
    assert list(Tenant.objects.values()) == before


def test_stale_final_review_is_rejected(st_invoice):
    review = review_st_delivery(st_invoice, recipients_to=["buyer@example.com"])
    models.QuerySet.update(type(st_invoice.company).objects.filter(pk=st_invoice.company_id), email_racunovodja="changed@example.com")
    with pytest.raises(ValidationError, match="zastarel"):
        prepare_st_attempt(st_invoice, review=review)
    assert not DeliveryAttempt.objects.exists()


def test_confirmed_snapshots_and_bcc_privacy_survive_master_changes(st_invoice):
    attempt = prepare(st_invoice)
    models.QuerySet.update(Tenant.objects.filter(pk=st_invoice.prejemnik_id), email="new@example.com")
    models.QuerySet.update(type(st_invoice.company).objects.filter(pk=st_invoice.company_id), email_racunovodja="new-accountant@example.com")
    prepared = build_pdf_email(attempt, subject="Račun", body="Račun v prilogi.")
    assert prepared.message["To"] == "Buyer@example.com"
    assert prepared.message["Cc"] == "copy@example.com"
    assert "Bcc" not in prepared.message
    assert prepared.envelope_recipients == ("Buyer@example.com", "copy@example.com", "accountant@example.com")
    attempt.sender = "new@example.com"
    with pytest.raises(ValidationError):
        attempt.save()


@pytest.mark.parametrize("change_company", [False, True])
def test_smtp_identity_mismatch_fails_before_credentials(st_invoice, smtp_config, change_company):
    attempt = prepare(st_invoice)
    config = replace(smtp_config, username=attempt.sender)
    if change_company:
        models.QuerySet.update(type(st_invoice.company).objects.filter(pk=st_invoice.company_id), email_posiljatelj="changed@example.com")
    else:
        config = replace(config, username="changed@example.com")
    with pytest.raises(ValidationError, match="SMTP"):
        send_pdf_email(attempt, subject="Račun", body="Račun", smtp_config=config, credential_ref=object(),
                       password_loader=lambda ref: pytest.fail("No credential access"))
    attempt.refresh_from_db()
    assert attempt.result == "PREPARED"


def test_configured_sender_domain_case_keeps_exact_smtp_identity(st_invoice, smtp_config):
    models.QuerySet.update(type(st_invoice.company).objects.filter(pk=st_invoice.company_id), email_posiljatelj="Sender@EXAMPLE.com")
    attempt = prepare(st_invoice)
    assert attempt.sender == "Sender@EXAMPLE.com"
    approve_smtp(st_invoice.company_id)
    assert send(attempt, smtp_config).result == "ACCEPTED"


@pytest.mark.parametrize("fault", ["missing", "corrupt", "draft", "unnumbered", "delivered"])
def test_send_fails_closed_before_network(st_invoice, smtp_config, settings, fault):
    attempt = prepare(st_invoice)
    artifact = settings.RENDERING_ROOT / attempt.pdf_path
    if fault == "missing":
        artifact.unlink()
    elif fault == "corrupt":
        artifact.write_bytes(b"corrupt")
    else:
        corrupt(st_invoice, **({"stevilka": None} if fault == "unnumbered" else {"status": "OSNUTEK" if fault == "draft" else "POSLAN"}))
    fake = FakeSmtp()
    with pytest.raises(ValidationError):
        send(attempt, smtp_config, fake)
    assert not fake.calls


def test_acceptance_once_preserves_number_financial_data_and_pdf(st_invoice, smtp_config, settings):
    before = type(st_invoice).objects.values().get(pk=st_invoice.pk)
    lines = list(st_invoice.invoiceline_set.values())
    numbers = list(NumberSequence.objects.values())
    artifact = settings.RENDERING_ROOT / st_invoice.rendered_pdf_path
    pdf = artifact.read_bytes()
    attempt, fake = prepare(st_invoice), FakeSmtp()
    assert send(attempt, smtp_config, fake).result == "ACCEPTED"
    with pytest.raises(ValidationError):
        send(attempt, smtp_config, fake)
    after = type(st_invoice).objects.values().get(pk=st_invoice.pk)
    assert after.pop("status") == "POSLAN"
    before.pop("status")
    assert before == after and lines == list(st_invoice.invoiceline_set.values())
    assert numbers == list(NumberSequence.objects.values()) and pdf == artifact.read_bytes()
    assert sum(call[0] == "data" for call in fake.calls) == 1
    assert not ArchivedFile.objects.exists() and not BackupSnapshot.objects.exists()


def test_confirmed_failure_retry_uses_frozen_snapshots_without_sending(st_invoice, smtp_config):
    source = send(prepare(st_invoice), smtp_config, FakeSmtp({"data": (550, b"rejected")}))
    st_invoice.refresh_from_db()
    assert source.result == "CONFIRMED_FAILURE" and st_invoice.status == "NAPAKA"
    retry = prepare_retry_attempt(source)
    for name in ("sender", "recipients_to", "recipients_cc", "recipients_bcc", "pdf_path", "pdf_checksum"):
        assert getattr(retry, name) == getattr(source, name)
    assert retry.result == "PREPARED" and retry.submission_started_at is None
    with pytest.raises(ValidationError):
        prepare_retry_attempt(source)
    with pytest.raises(ValidationError):
        send(source, smtp_config)
    assert send(retry, smtp_config).result == "ACCEPTED"


@pytest.mark.parametrize("decision", ["sent", "not_sent"])
@pytest.mark.parametrize("interrupted", [False, True])
def test_unknown_requires_reason_and_never_resends(st_invoice, smtp_config, decision, interrupted):
    source = prepare(st_invoice)
    if interrupted:
        mark_submission_started(source)
        assert recover_interrupted_submissions(invoice_ids=[st_invoice.pk]) == 1
        source.refresh_from_db()
    else:
        source = send(source, smtp_config, FakeSmtp({"data": TimeoutError()}))
    assert source.result == "UNKNOWN"
    before = DeliveryAttempt.objects.values().get(pk=source.pk)
    with pytest.raises(ValidationError):
        prepare_retry_attempt(source)
    with pytest.raises(ValidationError):
        send(source, smtp_config)
    resolver = resolve_unknown_as_sent if decision == "sent" else resolve_unknown_user_confirmed_not_sent_and_prepare_retry
    with pytest.raises(ValidationError):
        resolver(source, reason=" ")
    result = resolver(source, reason="Preverjena mapa Poslano in evidenca ponudnika.")
    assert DeliveryAttempt.objects.values().get(pk=source.pk) == before
    st_invoice.refresh_from_db()
    assert st_invoice.status == ("POSLAN" if decision == "sent" else "IZBRAN_ZA_POSILJANJE")
    assert AuditLogEntry.objects.filter(polje="unknown_resolution").count() == 1
    if decision == "not_sent":
        assert result.result == "PREPARED" and result.recipients_to == source.recipients_to
    with pytest.raises(ValidationError):
        resolver(source, reason="Ponovljena odločitev")


def test_prepare_and_send_reject_outer_transaction(st_invoice, smtp_config):
    attempt = prepare(st_invoice)
    with transaction.atomic(), pytest.raises(ValidationError):
        send(attempt, smtp_config)
    with transaction.atomic(), pytest.raises(ValidationError):
        review_st_delivery(st_invoice, recipients_to=["buyer@example.com"])


def test_simultaneous_preparations_have_one_winner(st_invoice):
    review = review_st_delivery(st_invoice, recipients_to=["buyer@example.com"])
    barrier = Barrier(2)

    def run():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            prepare_st_attempt(st_invoice, review=review)
            return 1
        except ValidationError:
            return 0
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(lambda _: run(), range(2))) == 1
    assert DeliveryAttempt.objects.count() == 1


def test_simultaneous_sends_submit_once(st_invoice, smtp_config):
    attempt = prepare(st_invoice)
    active, release = Event(), Event()

    def data(payload):
        active.set()
        assert release.wait(timeout=10)
        return 250, b"queued"

    fake = FakeSmtp({"data": data})

    def run():
        close_old_connections()
        try:
            return send(attempt, smtp_config, fake)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(run)
        try:
            assert active.wait(timeout=10)
            with pytest.raises(ValidationError, match="already being sent"):
                send(attempt, smtp_config)
            assert recover_interrupted_submissions(invoice_ids=[st_invoice.pk]) == 0
        finally:
            release.set()
        assert first.result(timeout=10).result == "ACCEPTED"
    assert sum(call[0] == "data" for call in fake.calls) == 1


@pytest.mark.parametrize("unknown", [False, True])
def test_corrupt_pdf_blocks_retry_preparation(st_invoice, smtp_config, settings, unknown):
    behavior = TimeoutError() if unknown else (550, b"rejected")
    source = send(prepare(st_invoice), smtp_config, FakeSmtp({"data": behavior}))
    (settings.RENDERING_ROOT / source.pdf_path).write_bytes(b"corrupt")
    with pytest.raises(ValidationError):
        if unknown:
            resolve_unknown_user_confirmed_not_sent_and_prepare_retry(source, reason="Preverjeno neposlano.")
        else:
            prepare_retry_attempt(source)
    assert DeliveryAttempt.objects.count() == 1
    assert not AuditLogEntry.objects.filter(polje="unknown_resolution").exists()


@pytest.mark.django_db(transaction=True)
def test_real_confirmed_st_pdf_uses_same_delivery_pipeline(confirmed, smtp_config):
    from rendering.services import render_pdf

    approve_smtp(confirmed.company_id, config=smtp_config)
    artifact = render_pdf(confirmed)
    attempt = prepare(confirmed)
    fake = FakeSmtp()
    assert send(attempt, smtp_config, fake).result == "ACCEPTED"
    payload = next(call[1] for call in fake.calls if call[0] == "data")
    assert artifact.checksum == attempt.pdf_checksum
    assert b"application/pdf" in payload and b"Bcc:" not in payload
    assert not ArchivedFile.objects.exists() and not BackupSnapshot.objects.exists()


_CHILD_ST_SEND = """
import os, sys
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'app.settings')
import django
django.setup()
from django.conf import settings
from django.db import connection
connection.settings_dict['NAME'] = sys.argv[1]
settings.MARJAN_SMTP_HOST = 'smtp.example.com'
settings.MARJAN_SMTP_PORT = 587
settings.MARJAN_SMTP_SECURITY = 'STARTTLS'
settings.MARJAN_SMTP_TIMEOUT_SECONDS = 12.5
settings.MARJAN_KEYRING_SERVICE = 'marjan'
settings.RENDERING_ROOT = __import__('pathlib').Path(sys.argv[2])
from delivery.models import DeliveryAttempt
from delivery.transport import SmtpConfig, SmtpSecurity
from tests.delivery.test_st_delivery import FakeSmtp, send
def data(payload):
    if sys.argv[4] == 'crash':
        os._exit(23)
    print('ACTIVE', flush=True)
    sys.stdin.readline()
    return 250, b'queued'
attempt = DeliveryAttempt.objects.get(pk=sys.argv[3])
config = SmtpConfig(host='smtp.example.com', port=587,
    security=SmtpSecurity.STARTTLS, timeout_seconds=12.5, username=attempt.sender)
send(attempt, config, FakeSmtp({'data': data}))
"""


@pytest.mark.parametrize("mode", ["active", "crash"])
def test_cross_process_lock_and_crash_recovery(st_invoice, smtp_config, settings, mode):
    from tests.delivery.test_crash_recovery import _wait_active

    attempt = prepare(st_invoice)
    args = [sys.executable, "-c", _CHILD_ST_SEND, str(connection.settings_dict["NAME"]),
            str(settings.RENDERING_ROOT), str(attempt.pk), mode]
    child = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    try:
        if mode == "active":
            _wait_active(child)
            with pytest.raises(ValidationError):
                cancel_st_attempt(st_invoice, attempt_id=attempt.pk,
                                  company_id=st_invoice.company_id, confirmed=True, reason="Ni dovoljeno")
            assert recover_interrupted_submissions(invoice_ids=[st_invoice.pk]) == 0
            fake = FakeSmtp()
            with pytest.raises(ValidationError, match="already being sent"):
                send(attempt, smtp_config, fake)
            assert not fake.calls
            child.stdin.write("finish\n")
            child.stdin.flush()
        _, error = child.communicate(timeout=20)
        assert child.returncode == (23 if mode == "crash" else 0), error
        with pytest.raises(ValidationError):
            cancel_st_attempt(st_invoice, attempt_id=attempt.pk,
                              company_id=st_invoice.company_id, confirmed=True, reason="Ni dovoljeno")
        assert recover_interrupted_submissions(invoice_ids=[st_invoice.pk]) == (mode == "crash")
        attempt.refresh_from_db()
        assert attempt.result == ("UNKNOWN" if mode == "crash" else "ACCEPTED")
        with pytest.raises(ValidationError):
            send(attempt, smtp_config)
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=20)
