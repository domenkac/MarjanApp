# ruff: noqa: F401, F811
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from queue import Queue
from threading import Barrier, Thread

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, transaction
from django.utils import timezone

from companies.models import NumberSequence
from core.models import AuditLogEntry
from delivery.models import DeliveryAttempt
from delivery.services import (
    mark_submission_started,
    prepare_retry_attempt,
    recover_interrupted_submissions,
    resolve_unknown_as_sent,
    resolve_unknown_user_confirmed_not_sent_and_prepare_retry,
)
from invoicing.models import Invoice, InvoiceLine
from tests.delivery.test_delivery_services import (
    FakeSmtp,
    _send,
    issued_invoice,
    prepared_email_data,
    smtp_config,
)

CHILD_SEND = """
import os, sys
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'app.settings')
import django
django.setup()
from django.conf import settings
from django.db import connection
connection.settings_dict['NAME'] = sys.argv[1]
settings.RENDERING_ROOT = __import__('pathlib').Path(sys.argv[2])
from delivery.models import DeliveryAttempt
from delivery.transport import SmtpConfig, SmtpSecurity
from tests.delivery.test_delivery_services import FakeSmtp, _send
mode = sys.argv[4]
def crash(*args):
    os._exit(23)
def active(*args):
    print('ACTIVE', flush=True)
    sys.stdin.readline()
    return 250, b'queued'
fake = FakeSmtp({'mail' if mode == 'before_data' else 'data':
                 active if mode == 'active' else crash})
config = SmtpConfig(host='smtp.example.com', port=587,
    security=SmtpSecurity.STARTTLS, timeout_seconds=12.5, username='smtp-user')
_send(DeliveryAttempt.objects.get(pk=sys.argv[3]), config, fake)
"""


def _child_args(attempt, settings, mode):
    return [
        sys.executable, "-c", CHILD_SEND, str(connection.settings_dict["NAME"]),
        str(settings.RENDERING_ROOT), str(attempt.pk), mode,
    ]


def _wait_active(child):
    output = Queue()
    Thread(target=lambda: output.put(child.stdout.readline()), daemon=True).start()
    assert output.get(timeout=20).strip() == "ACTIVE"


def _audits(attempt):
    return AuditLogEntry.objects.filter(
        content_type=ContentType.objects.get_for_model(DeliveryAttempt),
        object_id=attempt.pk, polje="result",
    )


@pytest.mark.parametrize("legacy", [False, True])
def test_recovery_preserves_snapshots_and_is_idempotent(prepared_email_data, legacy):
    invoice, attempt, artifact, pdf = prepared_email_data
    attempt = mark_submission_started(attempt)
    if legacy:
        DeliveryAttempt.objects.filter(pk=attempt.pk).update(execution_id=None)
    before = DeliveryAttempt.objects.values().get(pk=attempt.pk)
    invoice_before = Invoice.objects.values().get(pk=invoice.pk)
    lines_before = list(InvoiceLine.objects.filter(invoice=invoice).values())
    numbering_before = list(NumberSequence.objects.values())

    assert recover_interrupted_submissions() == 1
    assert recover_interrupted_submissions() == 0

    after = DeliveryAttempt.objects.values().get(pk=attempt.pk)
    for field in ("result", "result_code", "message", "completed_at"):
        after.pop(field)
        before.pop(field)
    assert after == before
    invoice_after = Invoice.objects.values().get(pk=invoice.pk)
    assert invoice_after.pop("status") == Invoice.Status.REZULTAT_NI_ZNAN
    invoice_before.pop("status")
    assert invoice_after == invoice_before
    assert list(InvoiceLine.objects.filter(invoice=invoice).values()) == lines_before
    assert list(NumberSequence.objects.values()) == numbering_before
    assert artifact.read_bytes() == pdf
    assert DeliveryAttempt.objects.count() == 1
    audit = _audits(attempt).get()
    assert (audit.stara_vrednost, audit.nova_vrednost) == ("SUBMISSION_STARTED", "UNKNOWN")
    assert "SMTP acceptance is unknown" in audit.razlog
    assert ("Legacy attempt" if legacy else str(attempt.execution_id)) in audit.razlog


@pytest.mark.parametrize("mode", ["before_data", "started"])
def test_process_crash_and_restart(prepared_email_data, settings, mode):
    invoice, attempt, _, _ = prepared_email_data
    child = subprocess.run(
        _child_args(attempt, settings, mode), capture_output=True, text=True,
        timeout=20, check=False,
    )
    assert child.returncode == 23, child.stderr
    connection.close()
    assert recover_interrupted_submissions() == (mode == "started")
    attempt.refresh_from_db()
    invoice.refresh_from_db()
    assert attempt.result == ("UNKNOWN" if mode == "started" else "PREPARED")
    assert invoice.status == (
        Invoice.Status.REZULTAT_NI_ZNAN if mode == "started"
        else Invoice.Status.IZBRAN_ZA_POSILJANJE
    )
    assert bool(attempt.execution_id) == (mode == "started")


def test_live_process_never_expires_or_allows_duplicate_send(
    prepared_email_data, settings, smtp_config,
):
    invoice, attempt, _, _ = prepared_email_data
    child = subprocess.Popen(
        _child_args(attempt, settings, "active"), stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        _wait_active(child)
        DeliveryAttempt.objects.filter(pk=attempt.pk).update(
            submission_started_at=timezone.now() - timedelta(days=365),
        )
        assert recover_interrupted_submissions() == 0
        fake = FakeSmtp()
        with pytest.raises(ValidationError, match="already being sent"):
            _send(attempt, smtp_config, fake)
        assert fake.calls == []
        assert not _audits(attempt).exists()
        child.stdin.write("finish\n")
        child.stdin.flush()
        _, stderr = child.communicate(timeout=20)
        assert child.returncode == 0, stderr
        invoice.refresh_from_db()
        assert invoice.status == Invoice.Status.POSLAN
        assert recover_interrupted_submissions() == 0
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=20)


def test_recovery_after_hard_kill_of_live_process(prepared_email_data, settings):
    _, attempt, _, _ = prepared_email_data
    child = subprocess.Popen(
        _child_args(attempt, settings, "active"), stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        _wait_active(child)
        assert recover_interrupted_submissions() == 0
    finally:
        child.kill()
        child.communicate(timeout=20)
    assert recover_interrupted_submissions() == 1
    assert recover_interrupted_submissions() == 0


def test_concurrent_recovery_recovers_once(prepared_email_data):
    _, attempt, _, _ = prepared_email_data
    mark_submission_started(attempt)
    barrier = Barrier(2)

    def recover():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return recover_interrupted_submissions()
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(lambda _: recover(), range(2))) == 1
    assert _audits(attempt).count() == 1


@pytest.mark.parametrize("decision", ["sent", "not_sent"])
def test_recovered_unknown_uses_explicit_resolution_only(prepared_email_data, decision):
    invoice, attempt, _, _ = prepared_email_data
    mark_submission_started(attempt)
    recover_interrupted_submissions()
    attempt.refresh_from_db()
    before = DeliveryAttempt.objects.values().get(pk=attempt.pk)
    with pytest.raises(ValidationError):
        prepare_retry_attempt(attempt)
    service = (
        resolve_unknown_as_sent if decision == "sent"
        else resolve_unknown_user_confirmed_not_sent_and_prepare_retry
    )
    with pytest.raises(ValidationError):
        service(attempt, reason=" ")
    result = service(attempt, reason="Uporabnik je preveril mapo Poslano.")
    assert DeliveryAttempt.objects.values().get(pk=attempt.pk) == before
    invoice.refresh_from_db()
    assert invoice.status == (
        Invoice.Status.POSLAN if decision == "sent" else Invoice.Status.IZBRAN_ZA_POSILJANJE
    )
    assert DeliveryAttempt.objects.count() == (1 if decision == "sent" else 2)
    if decision == "not_sent":
        assert result.result == "PREPARED"
        assert result.pdf_checksum == attempt.pdf_checksum
        assert result.recipients_to == attempt.recipients_to
        assert result.execution_id is None
    with pytest.raises(ValidationError):
        service(attempt, reason="Duplicate decision")
    assert recover_interrupted_submissions() == 0


def test_recovery_audit_failure_rolls_back_every_change(prepared_email_data, monkeypatch):
    invoice, attempt, _, _ = prepared_email_data
    mark_submission_started(attempt)
    before = DeliveryAttempt.objects.values().get(pk=attempt.pk)

    def fail(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(AuditLogEntry, "save", fail)
        with pytest.raises(RuntimeError, match="audit unavailable"):
            recover_interrupted_submissions()
    assert DeliveryAttempt.objects.values().get(pk=attempt.pk) == before
    invoice.refresh_from_db()
    assert invoice.status == Invoice.Status.IZBRAN_ZA_POSILJANJE
    assert not _audits(attempt).exists()
    assert recover_interrupted_submissions() == 1


def test_send_rejects_outer_transaction_before_network(prepared_email_data, smtp_config):
    _, attempt, _, _ = prepared_email_data
    fake = FakeSmtp()
    with transaction.atomic(), pytest.raises(ValidationError, match="outside a DB transaction"):
        _send(attempt, smtp_config, fake)
    assert not fake.calls


def test_recovery_ignores_other_invoices_and_conflicts(prepared_email_data):
    invoice, attempt, _, _ = prepared_email_data
    mark_submission_started(attempt)
    assert recover_interrupted_submissions(invoice_ids=[]) == 0
    Invoice.objects.filter(pk=invoice.pk).update(status=Invoice.Status.POSLAN)
    assert recover_interrupted_submissions() == 0
    assert not _audits(attempt).exists()


def test_acceptance_persistence_failure_recovers_as_unknown(
    prepared_email_data, smtp_config, monkeypatch,
):
    invoice, attempt, _, _ = prepared_email_data

    def fail(*args, **kwargs):
        raise RuntimeError("result persistence failed")

    fake = FakeSmtp()
    monkeypatch.setattr("delivery.services.mark_accepted", fail)
    with pytest.raises(RuntimeError, match="result persistence failed"):
        _send(attempt, smtp_config, fake)
    assert sum(call[0] == "data" for call in fake.calls) == 1
    assert recover_interrupted_submissions() == 1
    invoice.refresh_from_db()
    assert invoice.status == Invoice.Status.REZULTAT_NI_ZNAN
    with pytest.raises(ValidationError, match="prepared"):
        _send(attempt, smtp_config, FakeSmtp())


def test_concurrent_unknown_decisions_have_one_winner(prepared_email_data):
    _, attempt, _, _ = prepared_email_data
    mark_submission_started(attempt)
    recover_interrupted_submissions()
    barrier = Barrier(2)

    def resolve(service):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            service(attempt, reason="Explicit user decision")
            return 1
        except ValidationError:
            return 0
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(resolve, [
            resolve_unknown_as_sent,
            resolve_unknown_user_confirmed_not_sent_and_prepare_retry,
        ]))
    assert sum(results) == 1
    assert AuditLogEntry.objects.filter(polje="unknown_resolution").count() == 1
