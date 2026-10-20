"""Durable SMTP approval; all SMTP and credential adapters are synthetic."""
# ruff: noqa: F401, F811 -- Imported pytest fixtures.

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import pytest
from django.core.exceptions import ValidationError
from django.db import DatabaseError, close_old_connections

from companies.models import Company
from core.models import AuditLogEntry
from delivery.credentials import KeyringCredentialRef
from delivery.models import (
    DeliveryAttempt,
    MonthlySummaryConfiguration,
    SmtpVerification,
)
from delivery.services import (
    mark_confirmed_failure,
    mark_result_unknown,
    mark_submission_started,
    prepare_retry_attempt,
    resolve_unknown_as_sent,
    resolve_unknown_user_confirmed_not_sent_and_prepare_retry,
    send_pdf_email,
)
from delivery.transport import SmtpResult
from delivery.verification import (
    SmtpVerificationRequired,
    current_identity,
    require_verified,
    verification_status,
    verify_connection,
)
from tests.delivery.test_delivery_services import (
    FakeSmtp,
    issued_invoice,
    prepared_email_data,
    smtp_config,
)


def test_missing_verification_blocks_direct_send(prepared_email_data, smtp_config):
    _, attempt, _, _ = prepared_email_data
    SmtpVerification.objects.all().delete()
    calls = []
    with pytest.raises(ValidationError, match="SMTP"):
        send_pdf_email(
            attempt, subject="Invoice", body="Invoice", smtp_config=smtp_config,
            credential_ref=KeyringCredentialRef("marjan", "smtp-user"),
            password_loader=lambda _: calls.append("keychain") or "secret",
            smtp_factory=lambda *_: calls.append("network"),
        )
    assert calls == []
    attempt.refresh_from_db()
    assert attempt.result == DeliveryAttempt.Result.PREPARED


def verify(company_id, fake=None, **kwargs):
    return verify_connection(
        company_id, password_loader=lambda _: "top-secret",
        smtp_factory=lambda *_: fake or FakeSmtp(), **kwargs,
    )


def send(attempt, **kwargs):
    config, reference = current_identity(attempt.invoice.company_id)
    return send_pdf_email(
        attempt, subject="Invoice", body="Invoice", smtp_config=config,
        credential_ref=reference,
        password_loader=kwargs.pop("password_loader", lambda _: "top-secret"),
        smtp_factory=kwargs.pop("smtp_factory", lambda *_: FakeSmtp()), **kwargs,
    )


def test_success_is_durable_non_sending_and_audited(prepared_email_data):
    invoice, attempt, _, _ = prepared_email_data
    SmtpVerification.objects.all().delete()
    fake = FakeSmtp()
    assert verify(invoice.company_id, fake).success
    record = SmtpVerification.objects.get()
    assert record.verified_at and record.fingerprint and record.sender == attempt.sender
    assert record.result_code == "SMTP_CONNECTION_OK"
    assert AuditLogEntry.objects.filter(object_id=record.pk, polje="smtp_connection_test").exists()
    assert not any(call[0] in ("mail", "data", "rcpt_call") for call in fake.calls)
    assert verification_status(invoice.company_id).verified
    assert "top-secret" not in repr(SmtpVerification.objects.values().get())
    assert send(attempt).result == "ACCEPTED"


@pytest.mark.parametrize("earlier_success", [False, True])
def test_failed_retest_never_authorizes(prepared_email_data, earlier_success):
    invoice, attempt, _, _ = prepared_email_data
    if not earlier_success:
        SmtpVerification.objects.all().delete()
    failure = FakeSmtp({"login": OSError("provider secret")})
    assert not verify(invoice.company_id, failure).success
    with pytest.raises(SmtpVerificationRequired):
        send(attempt, smtp_factory=lambda *_: pytest.fail("network"))
    attempt.refresh_from_db()
    assert attempt.result == "PREPARED"
    assert "provider secret" not in repr(list(SmtpVerification.objects.values()))


@pytest.mark.parametrize("name,value", [
    ("MARJAN_SMTP_HOST", "changed.example.com"),
    ("MARJAN_SMTP_PORT", 465),
    ("MARJAN_SMTP_SECURITY", "SSL"),
    ("MARJAN_SMTP_TIMEOUT_SECONDS", 11),
    ("MARJAN_KEYRING_SERVICE", "other-service"),
])
def test_changed_transport_configuration_blocks(prepared_email_data, settings, name, value):
    invoice, attempt, _, _ = prepared_email_data
    setattr(settings, name, value)
    assert verification_status(invoice.company_id).state == "stale"
    with pytest.raises(SmtpVerificationRequired):
        send(attempt, password_loader=lambda _: pytest.fail("keychain"))


@pytest.mark.parametrize("field,value", [
    ("email_posiljatelj", "changed@example.com"),
    ("keychain_kljuc_posiljatelja", "rotated-reference"),
])
def test_identity_change_and_back_cannot_restore_proof(prepared_email_data, field, value):
    invoice, attempt, _, _ = prepared_email_data
    original = getattr(invoice.company, field)
    Company.objects.filter(pk=invoice.company_id).update(**{field: value})
    Company.objects.filter(pk=invoice.company_id).update(**{field: original})
    assert not verification_status(invoice.company_id).verified
    with pytest.raises(SmtpVerificationRequired):
        send(attempt)


def test_five_issuers_and_summary_are_independent(prepared_email_data):
    invoice, _, _, _ = prepared_email_data
    SmtpVerification.objects.all().delete()
    companies = [invoice.company]
    for index in range(4):
        company = Company.objects.get(pk=invoice.company_id)
        company.pk = None
        company.davcna_stevilka = f"1234567{index}"
        company.email_posiljatelj = f"issuer-{index}@example.com"
        company.keychain_kljuc_posiljatelja = f"key-{index}"
        company.save()
        companies.append(company)
    MonthlySummaryConfiguration.objects.create(email="owner@example.com", keychain_credential_key="owner")
    assert verify(None).success
    for company in companies:
        assert not verification_status(company.pk).verified
        config, reference = current_identity(company.pk)
        with pytest.raises(SmtpVerificationRequired):
            require_verified(company.pk, config, reference, config.username)
        assert verify(company.pk).success
        require_verified(company.pk, config, reference, config.username)
    assert SmtpVerification.objects.filter(verified_at__isnull=False).count() == 6
    config, reference = current_identity(companies[0].pk)
    with pytest.raises(SmtpVerificationRequired):
        require_verified(companies[1].pk, config, reference, config.username)


@pytest.mark.parametrize("field,value", [("email", "other@example.com"), ("keychain_credential_key", "other")])
def test_summary_identity_change_back_invalidates(prepared_email_data, field, value):
    configuration = MonthlySummaryConfiguration.objects.create(email="owner@example.com", keychain_credential_key="owner")
    assert verify(None).success
    MonthlySummaryConfiguration.objects.filter(pk=1).update(**{field: value})
    MonthlySummaryConfiguration.objects.filter(pk=1).update(**{field: getattr(configuration, field)})
    assert verification_status(None).state == "stale"


@pytest.mark.parametrize("delay", ["keychain", "rcpt"])
@pytest.mark.parametrize("change", ["reference", "host", "failed_retest"])
def test_change_during_external_delay_blocks_data(prepared_email_data, settings, delay, change):
    invoice, attempt, _, _ = prepared_email_data
    def mutate():
        if change == "reference":
            Company.objects.filter(pk=invoice.company_id).update(keychain_kljuc_posiljatelja="new-key")
        elif change == "host":
            settings.MARJAN_SMTP_HOST = "changed.example.com"
        else:
            verify(invoice.company_id, FakeSmtp({"login": OSError("failed")}))
    def password(_):
        mutate()
        return "top-secret"
    class WaitingSmtp(FakeSmtp):
        def rcpt(self, recipient):
            mutate()
            return 250, b"ok"
    fake = WaitingSmtp()
    with pytest.raises(SmtpVerificationRequired):
        send(attempt, password_loader=password if delay == "keychain" else lambda _: "top-secret",
             smtp_factory=(lambda *_: pytest.fail("network before recheck")) if delay == "keychain" else lambda *_: fake)
    attempt.refresh_from_db()
    assert attempt.result == "PREPARED" and attempt.submission_started_at is None
    assert not any(call[0] == "data" for call in fake.calls)


def test_superseded_success_cannot_override_failed_retest(prepared_email_data):
    invoice, _, _, _ = prepared_email_data
    def older_test(*_):
        assert not verify(invoice.company_id, FakeSmtp({"login": OSError("failed")})).success
        return SmtpResult(True, "SMTP_CONNECTION_OK")
    assert not verify_connection(invoice.company_id, tester=older_test).success
    assert not verification_status(invoice.company_id).verified


def test_configuration_change_during_test_rejects_even_return_to_original(prepared_email_data):
    invoice, _, _, _ = prepared_email_data
    def tester(*_):
        Company.objects.filter(pk=invoice.company_id).update(keychain_kljuc_posiljatelja="other")
        Company.objects.filter(pk=invoice.company_id).update(keychain_kljuc_posiljatelja="smtp-user")
        return SmtpResult(True, "SMTP_CONNECTION_OK")
    assert not verify_connection(invoice.company_id, tester=tester).success
    assert not verification_status(invoice.company_id).verified


def test_audit_write_failure_rolls_back_success_but_not_revocation(prepared_email_data, monkeypatch):
    invoice, attempt, _, _ = prepared_email_data
    def fail(*_, **__):
        raise DatabaseError("audit unavailable")
    monkeypatch.setattr(AuditLogEntry.objects, "create", fail)
    with pytest.raises(DatabaseError):
        verify(invoice.company_id)
    latest = SmtpVerification.objects.latest("pk")
    assert latest.verified_at is None and latest.result_code == "SMTP_TEST_STARTED"
    with pytest.raises(SmtpVerificationRequired):
        send(attempt)


@pytest.mark.parametrize("resolution", ["sent", "not_sent", "confirmed_failure"])
def test_recovery_decision_needs_no_approval_but_retry_send_does(prepared_email_data, resolution):
    _, attempt, _, _ = prepared_email_data
    SmtpVerification.objects.all().delete()
    if resolution == "confirmed_failure":
        retry = prepare_retry_attempt(mark_confirmed_failure(attempt, code="SMTP_CONNECT_FAILED"))
    else:
        unknown = mark_result_unknown(mark_submission_started(attempt), code="SMTP_RESULT_UNKNOWN")
        if resolution == "sent":
            resolve_unknown_as_sent(unknown, reason="Sent folder checked")
            return
        retry = resolve_unknown_user_confirmed_not_sent_and_prepare_retry(
            unknown, reason="Provider confirms no message submitted",
        )
    with pytest.raises(SmtpVerificationRequired):
        send(retry)
    retry.refresh_from_db()
    assert retry.result == "PREPARED"


def test_desktop_test_never_creates_approval_or_sends(prepared_email_data, settings):
    invoice, attempt, _, _ = prepared_email_data
    SmtpVerification.objects.all().delete()
    settings.MARJAN_DESKTOP_TEST = True
    assert not verify_connection(invoice.company_id, tester=lambda *_: pytest.fail("test adapter")).success
    assert not SmtpVerification.objects.exists()
    with pytest.raises(SmtpVerificationRequired):
        send(attempt, password_loader=lambda _: pytest.fail("keychain"))


def test_concurrent_retest_blocks_send_and_config_change_rejects_result(prepared_email_data):
    invoice, attempt, _, _ = prepared_email_data
    waiting, resume = Event(), Event()
    def tester(*_):
        waiting.set()
        assert resume.wait(10)
        return SmtpResult(True, "SMTP_CONNECTION_OK")
    def worker():
        close_old_connections()
        try:
            return verify_connection(invoice.company_id, tester=tester)
        finally:
            close_old_connections()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker)
        try:
            assert waiting.wait(10)
            with pytest.raises(SmtpVerificationRequired):
                send(attempt, password_loader=lambda _: pytest.fail("keychain"))
            Company.objects.filter(pk=invoice.company_id).update(keychain_kljuc_posiljatelja="new-key")
        finally:
            resume.set()
        assert not future.result(timeout=10).success
    assert not verification_status(invoice.company_id).verified
    attempt.refresh_from_db()
    assert attempt.result == "PREPARED"


@pytest.mark.parametrize("change", ["host", "username", "reference"])
def test_caller_cannot_substitute_transport_identity(prepared_email_data, change):
    invoice, attempt, _, _ = prepared_email_data
    config, reference = current_identity(invoice.company_id)
    if change == "reference":
        reference = KeyringCredentialRef(reference.service_name, "another-reference")
    else:
        config = replace(config, **{change: "other@example.com"})
    with pytest.raises(SmtpVerificationRequired):
        send_pdf_email(
            attempt, subject="Invoice", body="Invoice", smtp_config=config,
            credential_ref=reference, password_loader=lambda _: pytest.fail("keychain"),
        )


def test_invalid_configuration_retest_revokes_old_success(prepared_email_data, settings):
    invoice, _, _, _ = prepared_email_data
    original = settings.MARJAN_SMTP_HOST
    settings.MARJAN_SMTP_HOST = ""
    from delivery.configuration import SmtpConfigurationError

    with pytest.raises(SmtpConfigurationError):
        verify_connection(invoice.company_id, tester=lambda *_: pytest.fail("SMTP"))
    settings.MARJAN_SMTP_HOST = original
    assert not verification_status(invoice.company_id).verified


def test_start_record_failure_never_calls_transport_or_restores_approval(prepared_email_data, monkeypatch):
    invoice, attempt, _, _ = prepared_email_data
    def fail(*_, **__):
        raise DatabaseError("record unavailable")
    monkeypatch.setattr(SmtpVerification.objects, "create", fail)
    with pytest.raises(DatabaseError):
        verify_connection(invoice.company_id, tester=lambda *_: pytest.fail("SMTP"))
    assert not verification_status(invoice.company_id).verified
    with pytest.raises(SmtpVerificationRequired):
        send(attempt)
