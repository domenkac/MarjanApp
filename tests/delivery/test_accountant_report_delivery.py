# ruff: noqa: F401, F811
import hashlib
import smtplib
from copy import deepcopy
from datetime import date
from pathlib import Path

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from accounting_export import archive, csv_renderer
from accounting_export.models import AccountantReport, AccountantReportPdfArtifact
from archive.models import ArchivedFile
from companies.models import Company
from delivery.models import AccountantReportDeliveryAttempt as Attempt
from delivery.models import DeliveryAttempt
from delivery.services import (
    build_accountant_report_email,
    prepare_accountant_report_delivery,
    prepare_accountant_report_delivery_retry,
    send_accountant_report,
)
from delivery.transport import SmtpConfig, SmtpSecurity
from invoicing.models import Invoice, MonthlyCycle
from tests.accounting_export.test_csv_renderer import report

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def persisted(report):
    company = Company.objects.create(
        naziv="Live name", davcna_stevilka="15012557",
        email_posiljatelj="sender@example.com", email_racunovodja="accountant@example.com",
    )
    return AccountantReport.objects.create(
        company=company, period=date(2026, 7, 1), generated_at=timezone.now(),
        schema_version=1, snapshot=report.snapshot,
    )


@pytest.fixture
def archived(persisted, tmp_path, monkeypatch):
    monkeypatch.setattr(archive, "render_accountant_report_pdf", lambda _: b"%PDF-test")
    assert archive.archive_accountant_report_pdf(persisted, tmp_path).success
    return persisted


@pytest.fixture
def attempt(archived, tmp_path):
    return prepare_accountant_report_delivery(archived, local_root=tmp_path)


def _frozen(attempt):
    return {field: getattr(attempt, field) for field in (
        "sender", "recipient", "subject", "body", "attachment_filename", "attachment_checksum",
    )}


def _fail(attempt):
    Attempt.objects.filter(pk=attempt.pk).update(result=Attempt.Result.CONFIRMED_FAILURE)
    return attempt


def test_preparation_reload_history_and_frozen_email(archived, tmp_path):
    expected_csv = csv_renderer.render_accountant_report_csv(archived)
    expected_filename = csv_renderer.accountant_report_csv_filename(archived)
    archived.snapshot["header"]["company_name"] = "Caller mutation"
    archived.snapshot["header"]["period"] = "1999-01"
    attempt = prepare_accountant_report_delivery(archived, local_root=tmp_path)
    assert (attempt.sequence, attempt.result) == (1, Attempt.Result.PREPARED)
    assert attempt.sender == "sender@example.com"
    assert attempt.recipient == "accountant@example.com"
    assert attempt.subject == "Kontrolni dokument za Živa Alfa d.o.o. - julij - 2026"
    assert "Živa Alfa d.o.o." in attempt.body and "julij" in attempt.body and "2026" in attempt.body
    assert attempt.attachment_filename == expected_filename
    assert attempt.attachment_checksum == hashlib.sha256(expected_csv).hexdigest()
    Company.objects.filter(pk=archived.company_id).update(
        naziv="Edited", email_posiljatelj="new@example.com", email_racunovodja="new-to@example.com",
    )
    attempt.sender = "caller@example.com"
    prepared = build_accountant_report_email(attempt)
    assert prepared.envelope_sender == "sender@example.com"
    assert prepared.envelope_recipients == ("accountant@example.com",)
    message = prepared.message
    assert message["From"] == "sender@example.com"
    assert message["To"] == "accountant@example.com"
    assert message["Cc"] is None and message["Bcc"] is None
    attempt.refresh_from_db()
    assert message["Subject"] == attempt.subject
    assert message.get_body().get_content().rstrip() == attempt.body.rstrip()
    attachments = list(message.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_content_type() == "text/csv"
    assert attachments[0].get_filename() == expected_filename
    assert attachments[0].get_payload(decode=True) == expected_csv


@pytest.mark.parametrize("month,name", list(enumerate((
    "januar", "februar", "marec", "april", "maj", "junij", "julij", "avgust",
    "september", "oktober", "november", "december",
), 1)))
def test_month_names(persisted, tmp_path, monkeypatch, month, name):
    snapshot = deepcopy(persisted.snapshot)
    snapshot["header"]["period"] = f"2026-{month:02d}"
    current = AccountantReport.objects.create(
        company_id=persisted.company_id, period=date(2026, month, 1),
        generated_at=timezone.now(), schema_version=1, snapshot=snapshot,
    )
    monkeypatch.setattr(archive, "render_accountant_report_pdf", lambda _: b"%PDF-test")
    assert archive.archive_accountant_report_pdf(current, tmp_path).success
    attempt = prepare_accountant_report_delivery(current, local_root=tmp_path)
    assert attempt.subject == f"Kontrolni dokument za Živa Alfa d.o.o. - {name} - 2026"


def test_unsaved_and_duplicate(archived, report, tmp_path, attempt):
    for current in (report, AccountantReport(pk=archived.pk), archived):
        with pytest.raises(ValidationError):
            prepare_accountant_report_delivery(current, local_root=tmp_path)
    assert Attempt.objects.count() == 1


@pytest.mark.parametrize("field", ["email_posiljatelj", "email_racunovodja"])
def test_invalid_operational_address(archived, tmp_path, field):
    Company.objects.filter(pk=archived.company_id).update(**{field: "not an email"})
    with pytest.raises(ValidationError):
        prepare_accountant_report_delivery(archived, local_root=tmp_path)
    assert not Attempt.objects.exists()


def test_missing_artifact(persisted, tmp_path):
    with pytest.raises(ValidationError):
        prepare_accountant_report_delivery(persisted, local_root=tmp_path)
    assert not Attempt.objects.exists()


@pytest.mark.parametrize("damage", ["missing", "corrupt", "inaccessible", "root"])
@pytest.mark.parametrize("retry", [False, True])
def test_pdf_integrity_blocks_prepare(archived, tmp_path, monkeypatch, damage, retry):
    source = prepare_accountant_report_delivery(archived, local_root=tmp_path) if retry else None
    if retry:
        _fail(source)
    artifact = AccountantReportPdfArtifact.objects.get(report=archived)
    if damage == "missing":
        Path(artifact.local_path).unlink()
    elif damage == "corrupt":
        Path(artifact.local_path).write_bytes(b"corrupt")
    elif damage == "inaccessible":
        def inaccessible(_):
            raise PermissionError("inaccessible")
        monkeypatch.setattr(archive, "_hash_file", inaccessible)
    root = tmp_path / "other" if damage == "root" else tmp_path
    with pytest.raises(ValidationError):
        if retry:
            prepare_accountant_report_delivery_retry(source, local_root=root)
        else:
            prepare_accountant_report_delivery(archived, local_root=root)
    assert Attempt.objects.count() == int(retry)


def test_constraints_and_immutable_attempt(attempt):
    with pytest.raises(ValidationError):
        attempt.save()
    with pytest.raises(ValidationError):
        attempt.delete()
    for sequence in (0, 1):
        with pytest.raises(IntegrityError), transaction.atomic():
            Attempt.objects.create(report_id=attempt.report_id, sequence=sequence, **_frozen(attempt))
    Attempt.objects.filter(pk=attempt.pk).update(result=Attempt.Result.ACCEPTED)
    with pytest.raises(IntegrityError), transaction.atomic():
        Attempt.objects.create(
            report_id=attempt.report_id, sequence=2, result=Attempt.Result.ACCEPTED, **_frozen(attempt),
        )


@pytest.mark.parametrize("change", ["bytes", "filename"])
def test_renderer_drift_blocks_build_send_retry(attempt, tmp_path, monkeypatch, change):
    target = "render_accountant_report_csv" if change == "bytes" else "accountant_report_csv_filename"
    monkeypatch.setattr(csv_renderer, target, lambda _: b"changed" if change == "bytes" else "changed.csv")
    with pytest.raises(ValidationError, match="attachment"):
        build_accountant_report_email(attempt)
    with pytest.raises(ValidationError, match="attachment"):
        send_accountant_report(attempt, _config(), object(), smtp_factory=lambda *_: pytest.fail("SMTP"))
    _fail(attempt)
    with pytest.raises(ValidationError, match="attachment"):
        prepare_accountant_report_delivery_retry(attempt, local_root=tmp_path)
    assert Attempt.objects.count() == 1


def test_retry_frozen_and_latest(attempt, tmp_path):
    frozen = _frozen(attempt)
    Company.objects.filter(pk=attempt.report.company_id).update(
        email_posiljatelj="invalid", email_racunovodja="invalid",
    )
    retry = prepare_accountant_report_delivery_retry(_fail(attempt), local_root=tmp_path)
    assert retry.sequence == 2 and retry.result == Attempt.Result.PREPARED
    assert _frozen(retry) == frozen
    with pytest.raises(ValidationError):
        prepare_accountant_report_delivery_retry(attempt, local_root=tmp_path)
    with pytest.raises(ValidationError):
        prepare_accountant_report_delivery_retry(retry, local_root=tmp_path)
    assert Attempt.objects.count() == 2


@pytest.mark.parametrize("state", ["UNKNOWN", "ACCEPTED", "PREPARED", "SUBMISSION_STARTED"])
def test_no_send_or_retry_for_terminal_or_pending(attempt, tmp_path, state):
    Attempt.objects.filter(pk=attempt.pk).update(result=state)
    with pytest.raises(ValidationError):
        prepare_accountant_report_delivery_retry(attempt, local_root=tmp_path)
    if state != "PREPARED":
        with pytest.raises(ValidationError):
            send_accountant_report(attempt, _config(), object())
    assert Attempt.objects.count() == 1


def _config(username="sender@example.com"):
    return SmtpConfig("smtp.example.com", 587, SmtpSecurity.STARTTLS, 10, username)


class Smtp:
    def __init__(self, attempt, outcome):
        self.attempt = attempt
        self.outcome = outcome

    def ehlo(self):
        assert not connection.in_atomic_block

    def starttls(self, **kwargs):
        assert not connection.in_atomic_block

    def login(self, username, password):
        assert not connection.in_atomic_block
        assert password == "actual-password"

    def mail(self, sender):
        self.attempt.refresh_from_db()
        assert self.attempt.result == Attempt.Result.PREPARED
        return 250, b"ok"

    def rcpt(self, recipient):
        assert recipient == "accountant@example.com"
        return (550, b"denied actual-password token=hidden Bearer abc") if self.outcome == "recipient" else (250, b"ok")

    def data(self, payload):
        assert not connection.in_atomic_block
        self.attempt.refresh_from_db()
        assert self.attempt.result == Attempt.Result.SUBMISSION_STARTED
        assert self.attempt.submission_started_at is not None
        if self.outcome == "unknown":
            raise smtplib.SMTPServerDisconnected("gone")
        return (550 if self.outcome == "rejected" else 250), b"actual-password password=hidden Authorization: Bearer abc"

    def quit(self):
        pass

    def rset(self):
        pass


@pytest.mark.parametrize("outcome,state", [
    ("accepted", "ACCEPTED"), ("rejected", "CONFIRMED_FAILURE"),
    ("recipient", "CONFIRMED_FAILURE"), ("unknown", "UNKNOWN"),
])
def test_real_transport_result_and_domain_isolation(attempt, tmp_path, outcome, state):
    invoice = Invoice.objects.create(
        company_id=attempt.report.company_id, document_type="NP", stevilka="NP-00001",
        datum_izdaje=date(2026, 7, 1), obdobje_od=date(2026, 7, 1),
        obdobje_do=date(2026, 7, 31), status=Invoice.Status.POSLAN,
    )
    DeliveryAttempt.objects.create(
        invoice=invoice, sequence=1, sender="sender@example.com",
        recipients_to=["tenant@example.com"], pdf_path="invoice.pdf", pdf_checksum="a" * 64,
        result=DeliveryAttempt.Result.ACCEPTED,
    )
    MonthlyCycle.objects.create(
        obdobje=date(2026, 7, 1), datum_izdaje=date(2026, 7, 1),
        status=MonthlyCycle.Status.ZAKLJUCEN,
    )
    # Existing rows must remain byte-for-byte equal, not merely preserve counts.
    before = {model: list(model.objects.values()) for model in (
        Invoice, MonthlyCycle, DeliveryAttempt, AccountantReport, AccountantReportPdfArtifact, ArchivedFile,
    )}
    def password_loader(_):
        assert not connection.in_atomic_block
        return "actual-password"
    result = send_accountant_report(
        attempt, _config(), object(), password_loader=password_loader,
        smtp_factory=lambda *_: Smtp(attempt, outcome),
    )
    assert result.result == state and result.completed_at is not None
    assert (result.submission_started_at is not None) == (outcome != "recipient")
    assert "actual-password" not in result.message
    assert "hidden" not in result.message and "abc" not in result.message
    for model, rows in before.items():
        assert list(model.objects.values()) == rows
    with pytest.raises(ValidationError):
        send_accountant_report(result, _config(), object())


@pytest.mark.parametrize("config", [None, _config("other@example.com")])
def test_invalid_smtp_config_before_transport(attempt, monkeypatch, config):
    monkeypatch.setattr("delivery.transport.send_prepared_email", lambda *_args, **_kwargs: pytest.fail("transport"))
    with pytest.raises(ValidationError):
        send_accountant_report(attempt, config, object())
    attempt.refresh_from_db()
    assert attempt.result == Attempt.Result.PREPARED


@pytest.mark.parametrize("field", ["sender", "recipient"])
def test_frozen_addresses_validated(attempt, field):
    Attempt.objects.filter(pk=attempt.pk).update(**{field: "invalid"})
    with pytest.raises(ValidationError):
        build_accountant_report_email(attempt)


def test_outside_transactions_and_query_isolation(archived, tmp_path, monkeypatch):
    original_csv = csv_renderer.render_accountant_report_csv
    original_hash = archive._hash_file
    original_sha256 = hashlib.sha256
    def render(current):
        assert not connection.in_atomic_block
        return original_csv(current)
    def hash_file(path):
        assert not connection.in_atomic_block
        return original_hash(path)
    def sha256(*args, **kwargs):
        assert not connection.in_atomic_block
        return original_sha256(*args, **kwargs)
    monkeypatch.setattr(csv_renderer, "render_accountant_report_csv", render)
    monkeypatch.setattr(archive, "_hash_file", hash_file)
    monkeypatch.setattr(hashlib, "sha256", sha256)
    attempt = prepare_accountant_report_delivery(archived, local_root=tmp_path)
    with CaptureQueriesContext(connection) as queries:
        build_accountant_report_email(attempt)
    sql = " ".join(query["sql"] for query in queries).lower()
    for table in ("companies_company", "invoicing_invoice", "invoicing_invoiceline", "contracts_lease", "contracts_tenant", "catalogs_vatregime"):
        assert table not in sql
    with transaction.atomic(), pytest.raises(ValidationError):
        build_accountant_report_email(attempt)
    _fail(attempt)
    retry = prepare_accountant_report_delivery_retry(attempt, local_root=tmp_path)
    with transaction.atomic(), pytest.raises(ValidationError):
        prepare_accountant_report_delivery_retry(_fail(retry), local_root=tmp_path)


def test_first_prepare_reads_current_operational_settings(archived, tmp_path):
    # Populate the caller's relation cache before editing the stored company.
    assert archived.company.email_posiljatelj == "sender@example.com"
    Company.objects.filter(pk=archived.company_id).update(
        email_posiljatelj="current@example.com", email_racunovodja="current-to@example.com",
    )
    result = prepare_accountant_report_delivery(archived, local_root=tmp_path)
    assert (result.sender, result.recipient) == ("current@example.com", "current-to@example.com")


def test_first_prepare_rejects_enclosing_transaction(archived, tmp_path, monkeypatch):
    monkeypatch.setattr(csv_renderer, "render_accountant_report_csv", lambda _: pytest.fail("rendering"))
    with transaction.atomic(), pytest.raises(ValidationError, match="outside"):
        prepare_accountant_report_delivery(archived, local_root=tmp_path)
    assert not Attempt.objects.exists()


@pytest.mark.parametrize("race", ["attempt", "company", "report"])
def test_first_prepare_rechecks_races(archived, tmp_path, monkeypatch, race):
    original = csv_renderer.render_accountant_report_csv
    def render(current):
        data = original(current)
        if race == "attempt":
            Attempt.objects.create(
                report=current, sequence=1, sender="sender@example.com",
                recipient="accountant@example.com", subject="other", body="other",
                attachment_filename=csv_renderer.accountant_report_csv_filename(current),
                attachment_checksum=hashlib.sha256(data).hexdigest(),
            )
        elif race == "company":
            Company.objects.filter(pk=current.company_id).update(email_racunovodja="edited@example.com")
        else:
            snapshot = deepcopy(current.snapshot)
            snapshot["header"]["company_name"] = "Concurrent change"
            AccountantReport.objects.filter(pk=current.pk).update(snapshot=snapshot)
        return data
    monkeypatch.setattr(csv_renderer, "render_accountant_report_csv", render)
    with pytest.raises(ValidationError):
        prepare_accountant_report_delivery(archived, local_root=tmp_path)
    assert Attempt.objects.count() == int(race == "attempt")


def test_retry_rechecks_competing_retry(attempt, tmp_path, monkeypatch):
    original = csv_renderer.render_accountant_report_csv
    _fail(attempt)
    def render(current):
        data = original(current)
        Attempt.objects.create(report=current, sequence=2, **_frozen(attempt))
        return data
    monkeypatch.setattr(csv_renderer, "render_accountant_report_csv", render)
    with pytest.raises(ValidationError):
        prepare_accountant_report_delivery_retry(attempt, local_root=tmp_path)
    assert Attempt.objects.count() == 2


def test_submission_callback_race_never_sends_data(attempt):
    class RacingSmtp(Smtp):
        def rcpt(self, recipient):
            Attempt.objects.filter(pk=attempt.pk).update(result=Attempt.Result.UNKNOWN)
            return 250, b"ok"

        def data(self, payload):
            pytest.fail("DATA submitted after competing transition")

    with pytest.raises(ValidationError):
        send_accountant_report(
            attempt, _config(), object(), password_loader=lambda _: "actual-password",
            smtp_factory=lambda *_: RacingSmtp(attempt, "accepted"),
        )
    attempt.refresh_from_db()
    assert attempt.result == Attempt.Result.UNKNOWN


def test_unsaved_attempt_rejected(tmp_path):
    attempt = Attempt()
    for operation in (
        lambda: build_accountant_report_email(attempt),
        lambda: send_accountant_report(attempt, _config(), object()),
        lambda: prepare_accountant_report_delivery_retry(attempt, local_root=tmp_path),
    ):
        with pytest.raises(ValidationError):
            operation()


def test_prepare_retry_leave_report_and_pdf_unchanged(archived, tmp_path):
    models = (AccountantReport, AccountantReportPdfArtifact, ArchivedFile)
    before = {model: list(model.objects.values()) for model in models}
    artifact = AccountantReportPdfArtifact.objects.get(report=archived)
    path = Path(artifact.local_path)
    pdf_bytes, modified = path.read_bytes(), path.stat().st_mtime_ns
    first = prepare_accountant_report_delivery(archived, local_root=tmp_path)
    build_accountant_report_email(first)
    retry = prepare_accountant_report_delivery_retry(_fail(first), local_root=tmp_path)
    build_accountant_report_email(retry)
    for model in models:
        assert list(model.objects.values()) == before[model]
    assert path.read_bytes() == pdf_bytes and path.stat().st_mtime_ns == modified
