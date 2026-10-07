from datetime import date
from email import policy
from email.parser import BytesParser

import pytest
from django.db import connection
from django.utils import timezone

from accounting_export import archive
from accounting_export.models import AccountantReport, AccountantReportPdfArtifact
from accounting_export.services import create_accountant_report
from accounting_export.task_state import accountant_control_task_state
from companies.models import Company
from delivery.models import AccountantReportDeliveryAttempt as Attempt
from tests.accounting_export.test_report_snapshot import _invoice, _line
from tests.accounting_export.test_report_snapshot import (
    company as company,  # noqa: PLC0414
)

pytestmark = pytest.mark.django_db(transaction=True)
PERIOD = date(2026, 9, 1)
AS_OF = date(2026, 10, 1)


@pytest.fixture
def setup(company, tmp_path, settings, monkeypatch):
    from archive.models import ArchiveConfiguration

    _line(_invoice(company))
    ArchiveConfiguration.objects.create(
        local_root=str(tmp_path),
        icloud_root=str(tmp_path / "icloud"),
        verified_at=timezone.now(),
    )
    settings.MARJAN_SMTP_HOST = "smtp.example.com"
    settings.MARJAN_SMTP_PORT = 587
    settings.MARJAN_SMTP_SECURITY = "STARTTLS"
    settings.MARJAN_SMTP_TIMEOUT_SECONDS = 10
    settings.MARJAN_KEYRING_SERVICE = "test-service"
    monkeypatch.setattr(archive, "render_accountant_report_pdf", lambda _: b"%PDF-test")
    return company


class Smtp:
    def __init__(self, calls, outcome="accepted", before_data=None):
        self.calls = calls
        self.outcome = outcome
        self.before_data = before_data

    def ehlo(self):
        assert not connection.in_atomic_block

    def starttls(self, **kwargs):
        assert not connection.in_atomic_block

    def login(self, username, password):
        assert not connection.in_atomic_block
        assert password == "test-password"

    def mail(self, sender):
        return 250, b"ok"

    def rcpt(self, recipient):
        if self.before_data:
            self.before_data()
        return 250, b"ok"

    def data(self, payload):
        import smtplib

        assert not connection.in_atomic_block
        self.calls.append(BytesParser(policy=policy.default).parsebytes(payload))
        if self.outcome == "unknown":
            raise smtplib.SMTPServerDisconnected("secret path C:/private")
        return (550 if self.outcome == "failed" else 250), b"ok"

    def quit(self):
        pass

    def rset(self):
        pass


def run(company, calls, **kwargs):
    from accounting_export.workflow import deliver_accountant_control_report

    return deliver_accountant_control_report(
        company_id=company.pk,
        period=PERIOD,
        as_of=AS_OF,
        password_loader=kwargs.pop("password_loader", lambda _: "test-password"),
        smtp_factory=kwargs.pop("smtp_factory", lambda *_: Smtp(calls)),
        **kwargs,
    )


def test_not_sent_creates_one_csv_send_and_repeated_action_never_resends(setup):
    calls = []
    result = run(setup, calls)
    assert result.code == "ACCEPTED"
    assert (
        AccountantReport.objects.count()
        == AccountantReportPdfArtifact.objects.count()
        == 1
    )
    attempt = Attempt.objects.get()
    assert (attempt.sequence, attempt.result) == (1, "ACCEPTED")
    attachments = list(calls[0].iter_attachments())
    assert len(attachments) == 1 and attachments[0].get_content_type() == "text/csv"
    assert accountant_control_task_state(as_of=AS_OF).state == "ZAKLJUCENO"
    run(setup, calls)
    assert len(calls) == 1 and Attempt.objects.count() == 1


@pytest.mark.parametrize("stage", ["report", "pdf", "prepared"])
def test_resumes_persisted_stages(setup, tmp_path, stage):
    from delivery.services import prepare_accountant_report_delivery

    report = create_accountant_report(company=setup, period=PERIOD)
    if stage in ("pdf", "prepared"):
        assert archive.archive_accountant_report_pdf(report, tmp_path).success
    if stage == "prepared":
        attempt = prepare_accountant_report_delivery(report, local_root=tmp_path)
    result = run(setup, [])
    assert result.code == "ACCEPTED"
    assert AccountantReport.objects.get().pk == report.pk
    assert AccountantReportPdfArtifact.objects.count() == Attempt.objects.count() == 1
    if stage == "prepared":
        assert Attempt.objects.get().pk == attempt.pk


@pytest.mark.parametrize("state", ["UNKNOWN", "SUBMISSION_STARTED"])
@pytest.mark.parametrize("stale", [False, True])
def test_uncertain_delivery_blocks_even_stale(setup, tmp_path, state, stale):
    from delivery.services import prepare_accountant_report_delivery

    report = create_accountant_report(company=setup, period=PERIOD)
    archive.archive_accountant_report_pdf(report, tmp_path)
    attempt = prepare_accountant_report_delivery(report, local_root=tmp_path)
    Attempt.objects.filter(pk=attempt.pk).update(result=state)
    if stale:
        _line(_invoice(setup, number="ST-00002", kind="ST"))
    before = list(Attempt.objects.values())
    calls = []
    result = run(setup, calls)
    assert result.code == state
    assert not calls and AccountantReport.objects.count() == 1
    assert list(Attempt.objects.values()) == before


@pytest.mark.parametrize("state", ["ACCEPTED", "CONFIRMED_FAILURE", "PREPARED"])
def test_outdated_reports_replaced_without_mutating_history(setup, tmp_path, state):
    from delivery.services import prepare_accountant_report_delivery

    report = create_accountant_report(company=setup, period=PERIOD)
    archive.archive_accountant_report_pdf(report, tmp_path)
    attempt = prepare_accountant_report_delivery(report, local_root=tmp_path)
    Attempt.objects.filter(pk=attempt.pk).update(result=state)
    before = Attempt.objects.values().get(pk=attempt.pk)
    snapshot = report.snapshot
    _line(_invoice(setup, number="ST-00002", kind="ST"))
    calls = []
    assert run(setup, calls).code == "ACCEPTED"
    assert len(calls) == 1 and AccountantReport.objects.count() == 2
    assert AccountantReportPdfArtifact.objects.count() == Attempt.objects.count() == 2
    assert Attempt.objects.values().get(pk=attempt.pk) == before
    report.refresh_from_db()
    assert report.snapshot == snapshot
    fresh = AccountantReport.objects.exclude(pk=report.pk).get()
    assert {r["invoice_number"] for r in fresh.snapshot["issued_rows"]} == {
        "NP-00001",
        "ST-00002",
    }
    assert accountant_control_task_state(as_of=AS_OF).state == "ZAKLJUCENO"


def test_retry_unchanged_failure_freezes_metadata(setup, tmp_path):
    from delivery.services import prepare_accountant_report_delivery

    report = create_accountant_report(company=setup, period=PERIOD)
    archive.archive_accountant_report_pdf(report, tmp_path)
    attempt = prepare_accountant_report_delivery(report, local_root=tmp_path)
    Attempt.objects.filter(pk=attempt.pk).update(result="CONFIRMED_FAILURE")
    before = Attempt.objects.values().get(pk=attempt.pk)
    assert run(setup, []).code == "ACCEPTED"
    retry = Attempt.objects.get(sequence=2)
    assert retry.report_id == report.pk and AccountantReport.objects.count() == 1
    for field in (
        "sender",
        "recipient",
        "subject",
        "body",
        "attachment_filename",
        "attachment_checksum",
    ):
        assert getattr(retry, field) == before[field]
    assert Attempt.objects.values().get(pk=attempt.pk) == before


def test_two_companies_only_chosen_company_sent(setup):
    other = Company.objects.create(naziv="Other", davcna_stevilka="87654321")
    _invoice(other)
    assert run(setup, []).code == "ACCEPTED"
    task = accountant_control_task_state(as_of=AS_OF)
    assert task.state == "DELNO"
    assert {r.company_id: r.state for r in task.companies} == {
        setup.pk: "CURRENT",
        other.pk: "NOT_SENT",
    }


def prepared(company, root):
    from delivery.services import prepare_accountant_report_delivery

    report = create_accountant_report(company=company, period=PERIOD)
    assert archive.archive_accountant_report_pdf(report, root).success
    return prepare_accountant_report_delivery(report, local_root=root)


def test_missing_archive_configuration_fails_before_creation(setup):
    from archive.models import ArchiveConfiguration

    ArchiveConfiguration.objects.all().delete()
    calls = []
    assert run(setup, calls).code == "ARCHIVE_CONFIGURATION"
    assert not calls and not AccountantReport.objects.exists()


@pytest.mark.parametrize("damage", ["missing", "corrupt", "metadata"])
def test_corrupt_archive_not_bypassed(setup, tmp_path, damage):
    from pathlib import Path

    attempt = prepared(setup, tmp_path)
    artifact = AccountantReportPdfArtifact.objects.get()
    path = Path(artifact.local_path)
    if damage == "missing":
        path.unlink()
    elif damage == "corrupt":
        path.write_bytes(b"corrupt")
    else:
        # Simulate out-of-band DB corruption, bypassing immutable manager.
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE accounting_export_accountantreportpdfartifact SET checksum=%s",
                ["invalid"],
            )
    calls = []
    result = run(setup, calls)
    assert result.code in (
        "ARCHIVE_MISSING",
        "ARCHIVE_CHECKSUM_MISMATCH",
        "ARCHIVE_METADATA_CONFLICT",
    )
    assert (
        not calls
        and AccountantReport.objects.count() == 1
        and Attempt.objects.count() == 1
    )
    attempt.refresh_from_db()
    assert attempt.result == "PREPARED"


def test_reverify_pdf_and_currentness_before_smtp(setup, tmp_path, monkeypatch):
    from pathlib import Path

    from accounting_export import workflow

    original = workflow.resolve_smtp_config_for_username

    def corrupt_after_preparation(username):
        if Attempt.objects.exists():
            Path(AccountantReportPdfArtifact.objects.get().local_path).write_bytes(
                b"corrupt"
            )
        return original(username)

    monkeypatch.setattr(
        workflow, "resolve_smtp_config_for_username", corrupt_after_preparation
    )
    calls = []
    assert run(setup, calls).code == "ARCHIVE_CHECKSUM_MISMATCH"
    assert not calls and Attempt.objects.get().result == "PREPARED"


def test_new_invoice_before_final_check_leaves_attempt_prepared(setup, monkeypatch):
    from accounting_export import workflow

    original = workflow.resolve_smtp_config_for_username

    def changed(username):
        if Attempt.objects.exists():
            _line(_invoice(setup, number="ST-00002", kind="ST"))
        return original(username)

    monkeypatch.setattr(workflow, "resolve_smtp_config_for_username", changed)
    calls = []
    assert run(setup, calls).code == "STALE"
    assert not calls and Attempt.objects.get().result == "PREPARED"
    assert AccountantReport.objects.count() == 1


def test_configuration_failure_resumes_same_prepared_attempt(setup, settings, tmp_path):
    original = prepared(setup, tmp_path)
    settings.MARJAN_SMTP_HOST = ""
    calls = []
    assert run(setup, calls).code == "SMTP_CONFIGURATION"
    attempt = Attempt.objects.get()
    assert attempt.pk == original.pk
    assert attempt.result == "PREPARED" and not calls
    settings.MARJAN_SMTP_HOST = "smtp.example.com"
    assert run(setup, calls).code == "ACCEPTED"
    assert (
        Attempt.objects.get().pk == attempt.pk and AccountantReport.objects.count() == 1
    )


@pytest.mark.parametrize("field", ["email_posiljatelj", "email_racunovodja"])
def test_invalid_addresses_never_send(setup, field, tmp_path):
    Company.objects.filter(pk=setup.pk).update(**{field: "invalid-address"})
    calls = []
    assert run(setup, calls).code == "PREPARATION_FAILED"
    assert not calls and not Attempt.objects.exists()
    assert not AccountantReport.objects.exists()
    assert not AccountantReportPdfArtifact.objects.exists()
    assert not list(tmp_path.rglob("*.pdf"))


@pytest.mark.parametrize(
    "failure", ["sender", "recipient", "smtp", "keyring_service", "credential_key"]
)
@pytest.mark.parametrize("existing_report", [False, True])
def test_first_attempt_preflight_has_no_preparation_side_effects(
    setup,
    tmp_path,
    settings,
    monkeypatch,
    failure,
    existing_report,
):
    from accounting_export import workflow

    if existing_report:
        report = create_accountant_report(company=setup, period=PERIOD)
    if failure == "sender":
        Company.objects.filter(pk=setup.pk).update(email_posiljatelj="invalid")
    elif failure == "recipient":
        Company.objects.filter(pk=setup.pk).update(email_racunovodja="invalid")
    elif failure == "smtp":
        settings.MARJAN_SMTP_HOST = ""
    elif failure == "keyring_service":
        settings.MARJAN_KEYRING_SERVICE = ""
    else:
        Company.objects.filter(pk=setup.pk).update(keychain_kljuc_posiljatelja="")

    def forbidden(*args, **kwargs):
        pytest.fail(
            "preparation, password lookup or SMTP before valid basic configuration"
        )

    monkeypatch.setattr(workflow, "archive_accountant_report_pdf", forbidden)
    result = run(setup, [], password_loader=forbidden, smtp_factory=forbidden)
    assert result.code == (
        "PREPARATION_FAILED"
        if failure in ("sender", "recipient")
        else "SMTP_CONFIGURATION"
    )
    assert AccountantReport.objects.count() == int(existing_report)
    if existing_report:
        assert AccountantReport.objects.get().pk == report.pk
    assert (
        not AccountantReportPdfArtifact.objects.exists()
        and not Attempt.objects.exists()
    )
    assert not list(tmp_path.rglob("*.pdf"))


@pytest.mark.parametrize("state", ["PREPARED", "CONFIRMED_FAILURE"])
def test_frozen_recipient_survives_invalid_live_recipient(setup, tmp_path, state):
    first = prepared(setup, tmp_path)
    Attempt.objects.filter(pk=first.pk).update(result=state)
    before = Attempt.objects.values().get(pk=first.pk)
    Company.objects.filter(pk=setup.pk).update(
        email_racunovodja="invalid-live-recipient"
    )
    calls = []
    assert run(setup, calls).code == "ACCEPTED"
    sent = Attempt.objects.get(result="ACCEPTED")
    assert sent.recipient == before["recipient"] == "rac@example.com"
    assert str(calls[0]["To"]) == "rac@example.com"
    assert AccountantReport.objects.count() == 1
    if state == "PREPARED":
        assert sent.pk == first.pk
    else:
        assert sent.sequence == first.sequence + 1
        assert Attempt.objects.values().get(pk=first.pk) == before


def test_changed_sender_blocks_before_password_or_network(setup, tmp_path):
    attempt = prepared(setup, tmp_path)
    Company.objects.filter(pk=setup.pk).update(email_posiljatelj="new@example.com")

    def forbidden(*args):
        pytest.fail("credentials or network accessed for changed sender")

    assert (
        run(setup, [], password_loader=forbidden, smtp_factory=forbidden).code
        == "SENDER_CHANGED"
    )
    attempt.refresh_from_db()
    assert attempt.result == "PREPARED"


def test_same_sender_uses_current_credential_reference(setup, tmp_path):
    attempt = prepared(setup, tmp_path)
    Company.objects.filter(pk=setup.pk).update(keychain_kljuc_posiljatelja="new-key")
    keys = []

    def password(ref):
        assert not connection.in_atomic_block
        keys.append(ref)
        return "test-password"

    calls = []
    assert run(setup, calls, password_loader=password).code == "ACCEPTED"
    assert len(keys) == 1 and keys[0].username == "new-key"
    assert Attempt.objects.get().pk == attempt.pk


def test_credential_error_feedback_is_sanitized(setup):
    def password(_):
        raise RuntimeError("password=hunter2 C:/private/keychain")

    calls = []
    result = run(setup, calls, password_loader=password)
    assert result.code == "CREDENTIAL_FAILED" and not calls
    assert Attempt.objects.get().result == "PREPARED"
    assert "hunter2" not in result.message and "C:/private" not in result.message


@pytest.mark.parametrize(
    "change,expected",
    [
        ("issued", "STALE"),
        ("sender", "SENDER_CHANGED"),
        ("pdf", "ARCHIVE_CHECKSUM_MISMATCH"),
        ("pending", "SUBMISSION_STARTED"),
    ],
)
def test_changes_while_keychain_waits_block_before_network(setup, change, expected):
    from pathlib import Path

    calls = []

    def password(_):
        if change == "issued":
            _line(_invoice(setup, number="ST-00002", kind="ST"))
        elif change == "sender":
            Company.objects.filter(pk=setup.pk).update(
                email_posiljatelj="edited@example.com"
            )
        elif change == "pdf":
            Path(AccountantReportPdfArtifact.objects.get().local_path).write_bytes(
                b"corrupt"
            )
        else:
            Attempt.objects.update(result="SUBMISSION_STARTED")
        return "test-password"

    def network(*_):
        pytest.fail("network entered after evidence changed during Keychain lookup")

    result = run(setup, calls, password_loader=password, smtp_factory=network)
    assert result.code == expected and not calls
    assert Attempt.objects.get().result == (
        "SUBMISSION_STARTED" if change == "pending" else "PREPARED"
    )


@pytest.mark.parametrize(
    "outcome,expected", [("failed", "CONFIRMED_FAILURE"), ("unknown", "UNKNOWN")]
)
def test_transport_failure_derived_dashboard_state(setup, outcome, expected):
    calls = []
    assert (
        run(setup, calls, smtp_factory=lambda *_: Smtp(calls, outcome)).code == expected
    )
    assert len(calls) == 1
    assert (
        accountant_control_task_state(as_of=AS_OF).companies[0].state
        == {
            "CONFIRMED_FAILURE": "FAILED",
            "UNKNOWN": "UNKNOWN",
        }[expected]
    )


def test_competing_transition_before_data_never_sends(setup):
    def competing():
        Attempt.objects.filter(result="PREPARED").update(result="SUBMISSION_STARTED")

    calls = []
    result = run(
        setup, calls, smtp_factory=lambda *_: Smtp(calls, before_data=competing)
    )
    assert result.code == "SUBMISSION_STARTED"
    assert not calls and Attempt.objects.get().result == "SUBMISSION_STARTED"


@pytest.mark.parametrize("stage", ["after_report", "after_pdf", "after_publication"])
def test_partial_failure_resumes_without_duplicates(
    setup, tmp_path, monkeypatch, stage
):
    from django.db import IntegrityError

    from accounting_export import workflow

    original_archive = workflow.archive_accountant_report_pdf
    original_prepare = workflow.prepare_accountant_report_delivery
    original_metadata = AccountantReportPdfArtifact.objects.get_or_create

    def fail(*args, **kwargs):
        raise IntegrityError("interrupted")

    if stage == "after_report":
        monkeypatch.setattr(workflow, "archive_accountant_report_pdf", fail)
    elif stage == "after_pdf":
        monkeypatch.setattr(workflow, "prepare_accountant_report_delivery", fail)
    else:
        monkeypatch.setattr(AccountantReportPdfArtifact.objects, "get_or_create", fail)
    assert run(setup, []).code == "PREPARATION_FAILED"
    report_id = AccountantReport.objects.get().pk
    monkeypatch.setattr(workflow, "archive_accountant_report_pdf", original_archive)
    monkeypatch.setattr(
        workflow, "prepare_accountant_report_delivery", original_prepare
    )
    monkeypatch.setattr(
        AccountantReportPdfArtifact.objects, "get_or_create", original_metadata
    )
    assert run(setup, []).code == "ACCEPTED"
    assert AccountantReport.objects.get().pk == report_id
    assert AccountantReportPdfArtifact.objects.count() == Attempt.objects.count() == 1
    assert not (tmp_path / "icloud").exists()


def test_domain_rows_unchanged_by_workflow(setup):
    from archive.models import ArchivedFile
    from delivery.models import DeliveryAttempt, MonthlySummaryAttempt
    from invoicing.models import Invoice, MonthlyBatch, MonthlyCycle

    DeliveryAttempt.objects.create(
        invoice=Invoice.objects.get(company=setup),
        sequence=1,
        sender="tenant-sender@example.com",
        recipients_to=["tenant@example.com"],
        pdf_path="invoice.pdf",
        pdf_checksum="a" * 64,
        result="ACCEPTED",
    )
    MonthlySummaryAttempt.objects.create(
        cycle=MonthlyCycle.objects.get(obdobje=PERIOD),
        sequence=1,
        sender="summary@example.com",
        recipient="owner@example.com",
        subject="Summary",
        body="Summary",
        summary_snapshot={"frozen": True},
        result="ACCEPTED",
    )
    models = (
        Invoice,
        MonthlyCycle,
        MonthlyBatch,
        DeliveryAttempt,
        MonthlySummaryAttempt,
        ArchivedFile,
    )
    before = {model: list(model.objects.values()) for model in models}
    assert run(setup, []).code == "ACCEPTED"
    assert before == {model: list(model.objects.values()) for model in models}


def test_no_issued_invoice_preserves_accounting_one_rejection(setup):
    from invoicing.models import Invoice

    Invoice.objects.update(status=Invoice.Status.OSNUTEK)
    result = run(setup, [])
    assert result.code == "NO_ISSUED_SNAPSHOT"
    assert not AccountantReport.objects.exists()


def test_only_latest_failure_retried(setup, tmp_path):
    from delivery.services import prepare_accountant_report_delivery_retry

    first = prepared(setup, tmp_path)
    Attempt.objects.filter(pk=first.pk).update(result="CONFIRMED_FAILURE")
    second = prepare_accountant_report_delivery_retry(first, local_root=tmp_path)
    Attempt.objects.filter(pk=second.pk).update(result="CONFIRMED_FAILURE")
    assert run(setup, []).code == "ACCEPTED"
    assert list(Attempt.objects.values_list("sequence", "result")) == [
        (1, "CONFIRMED_FAILURE"),
        (2, "CONFIRMED_FAILURE"),
        (3, "ACCEPTED"),
    ]


def test_unknown_on_older_report_blocks_prepared_replacement(setup, tmp_path):
    older = prepared(setup, tmp_path)
    Attempt.objects.filter(pk=older.pk).update(result="UNKNOWN")
    _line(_invoice(setup, number="ST-00002", kind="ST"))
    newer = prepared(setup, tmp_path)
    before = list(Attempt.objects.values())
    calls = []
    assert run(setup, calls).code == "UNKNOWN"
    assert not calls and list(Attempt.objects.values()) == before
    assert newer.result == "PREPARED" and AccountantReport.objects.count() == 2


def test_concurrent_initial_actions_share_one_report_and_one_smtp_data(
    setup, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from django.db import connections

    from accounting_export import workflow

    barrier = Barrier(2)
    original = workflow._choose_report

    def choose(*args):
        barrier.wait(timeout=10)
        return original(*args)

    monkeypatch.setattr(workflow, "_choose_report", choose)
    calls = []

    def action():
        try:
            return run(setup, calls)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(action) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]
    assert any(result.code == "ACCEPTED" for result in results)
    assert len(calls) == 1
    assert (
        AccountantReport.objects.count()
        == AccountantReportPdfArtifact.objects.count()
        == 1
    )
    assert (
        Attempt.objects.filter(result="ACCEPTED").count()
        == Attempt.objects.count()
        == 1
    )
