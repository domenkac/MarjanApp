"""Prepared-only ST cancellation uses isolated databases and mocked transport."""
# ruff: noqa: F401, F811 -- Imported pytest fixtures.

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Barrier, Event
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, transaction
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from archive.models import ArchivedFile, BackupSnapshot
from companies.models import Company, NumberSequence
from core.models import AuditLogEntry
from delivery.execution import delivery_execution_lock
from delivery.models import DeliveryAttempt
from delivery.services import (
    build_pdf_email,
    mark_submission_started,
    prepare_retry_attempt,
    recover_interrupted_submissions,
)
from delivery.st_delivery import (
    cancel_st_attempt,
    prepare_st_attempt,
    review_st_delivery,
)
from invoicing.models import Invoice
from tests.delivery.test_confirmed_failure_migration import isolated_migration_db
from tests.delivery.test_delivery_services import FakeSmtp, issued_invoice, smtp_config
from tests.delivery.test_st_delivery import corrupt, prepare, send, st_invoice


def cancel(invoice, attempt, **changes):
    return cancel_st_attempt(invoice, **({
        "attempt_id": attempt.pk, "company_id": invoice.company_id,
        "confirmed": True, "reason": "Napačen prejemnik. Potreben je nov pregled.",
    } | changes))


def test_cancellation_preserves_invoice_pdf_and_frozen_history(st_invoice, settings):
    attempt = prepare(st_invoice)
    invoice_before = type(st_invoice).objects.values().get(pk=st_invoice.pk)
    lines = list(st_invoice.invoiceline_set.values())
    numbers = list(NumberSequence.objects.values())
    original = DeliveryAttempt.objects.values().get(pk=attempt.pk)
    artifact = settings.RENDERING_ROOT / attempt.pdf_path
    pdf = artifact.read_bytes()
    cancelled = cancel(st_invoice, attempt)
    after = DeliveryAttempt.objects.values().get(pk=attempt.pk)
    assert after.pop("result") == "CANCELLED"
    assert after.pop("completed_at") is not None
    original.pop("result")
    original.pop("completed_at")
    assert original == after
    assert type(st_invoice).objects.values().get(pk=st_invoice.pk) == invoice_before
    assert list(st_invoice.invoiceline_set.values()) == lines
    assert list(NumberSequence.objects.values()) == numbers
    assert artifact.read_bytes() == pdf
    assert DeliveryAttempt.objects.count() == 1
    audit = AuditLogEntry.objects.get(polje="result", object_id=attempt.pk)
    assert (audit.stara_vrednost, audit.nova_vrednost) == ("PREPARED", "CANCELLED")
    evidence = json.loads(audit.razlog)
    assert evidence["reason"] == "Napačen prejemnik. Potreben je nov pregled."
    assert evidence["invoice_id"] == st_invoice.pk
    assert evidence["company_id"] == st_invoice.company_id
    assert evidence["attempt_id"] == attempt.pk
    assert evidence["cancelled_at"] == cancelled.completed_at.isoformat()
    assert audit.created_at >= cancelled.completed_at
    for field in ("sender", "recipients_to", "recipients_cc", "recipients_bcc", "pdf_path", "pdf_checksum", "sequence"):
        assert evidence[field] == getattr(attempt, field)
    with pytest.raises(ValidationError):
        cancelled.save()
    with pytest.raises(ValidationError):
        cancelled.delete()
    assert not ArchivedFile.objects.exists() and not BackupSnapshot.objects.exists()


@pytest.mark.parametrize("changes", [
    {"reason": ""}, {"reason": "  "}, {"reason": None},
    {"confirmed": False}, {"confirmed": "true"}, {"confirmed": 1},
    {"company_id": -1}, {"attempt_id": -1},
])
def test_invalid_cancellation_has_no_effect(st_invoice, changes):
    attempt = prepare(st_invoice)
    before = DeliveryAttempt.objects.values().get(pk=attempt.pk)
    with pytest.raises(ValidationError):
        cancel(st_invoice, attempt, **changes)
    assert DeliveryAttempt.objects.values().get(pk=attempt.pk) == before
    assert not AuditLogEntry.objects.exists()


@pytest.mark.parametrize("result", ["SUBMISSION_STARTED", "ACCEPTED", "CONFIRMED_FAILURE", "UNKNOWN", "CANCELLED"])
def test_nonprepared_attempt_cannot_be_cancelled(st_invoice, result):
    attempt = prepare(st_invoice)
    DeliveryAttempt.objects.filter(pk=attempt.pk).update(result=result)
    with pytest.raises(ValidationError):
        cancel(st_invoice, attempt)
    assert not AuditLogEntry.objects.exists()


@pytest.mark.parametrize("changes", [
    {"status": "POSLAN"}, {"status": "NAPAKA"}, {"status": "REZULTAT_NI_ZNAN"},
    {"status": "OSNUTEK"}, {"stevilka": None}, {"document_type": "NP"},
    {"povezan_racun_id": "self"}, {"lease_id": "original"},
])
def test_changed_or_nonstandalone_invoice_cannot_be_cancelled(st_invoice, changes):
    attempt = prepare(st_invoice)
    changes = {
        field: st_invoice.pk if value == "self" else (
            st_invoice.prejemnik.lease_set.first().pk if value == "original" else value
        ) for field, value in changes.items()
    }
    corrupt(st_invoice, **changes)
    with pytest.raises(ValidationError):
        cancel(st_invoice, attempt)
    attempt.refresh_from_db()
    assert attempt.result == "PREPARED"


@pytest.mark.parametrize("field", ["submission_started_at", "execution_id"])
def test_submission_evidence_blocks_cancellation_even_if_result_is_prepared(st_invoice, field):
    from uuid import uuid4

    attempt = prepare(st_invoice)
    DeliveryAttempt.objects.filter(pk=attempt.pk).update(**{
        field: timezone.now() if field == "submission_started_at" else uuid4(),
    })
    with pytest.raises(ValidationError):
        cancel(st_invoice, attempt)


def test_audit_failure_rolls_back_cancellation(st_invoice):
    attempt = prepare(st_invoice)
    with patch.object(AuditLogEntry, "save", side_effect=RuntimeError("audit failed")), pytest.raises(RuntimeError, match="audit failed"):
        cancel(st_invoice, attempt)
    attempt.refresh_from_db()
    assert attempt.result == "PREPARED" and attempt.completed_at is None
    assert not AuditLogEntry.objects.exists()


def test_attempt_from_another_invoice_is_rejected(st_invoice):
    attempt = prepare(st_invoice)
    other = type(st_invoice).objects.create(
        company=st_invoice.company, document_type="ST", stevilka="ST-2026-0002",
        datum_izdaje=st_invoice.datum_izdaje,
        obdobje_od=st_invoice.obdobje_od, obdobje_do=st_invoice.obdobje_do,
        status="IZBRAN_ZA_POSILJANJE",
    )
    with pytest.raises(ValidationError):
        cancel(other, attempt)
    attempt.refresh_from_db()
    assert attempt.result == "PREPARED" and not AuditLogEntry.objects.exists()


def test_nonlatest_prepared_attempt_is_rejected(st_invoice):
    attempt = prepare(st_invoice)
    # Synthetic inconsistent history tests the latest-attempt guard independently.
    replacement = DeliveryAttempt.objects.create(
        invoice=st_invoice, sequence=2, sender=attempt.sender,
        recipients_to=attempt.recipients_to, recipients_bcc=attempt.recipients_bcc,
        pdf_path=attempt.pdf_path, pdf_checksum=attempt.pdf_checksum,
    )
    with pytest.raises(ValidationError):
        cancel(st_invoice, attempt)
    attempt.refresh_from_db()
    assert attempt.result == "PREPARED"
    assert cancel(st_invoice, replacement).result == "CANCELLED"


def test_cancellation_rejects_outer_transaction_and_busy_execution_lock(st_invoice):
    attempt = prepare(st_invoice)
    with transaction.atomic(), pytest.raises(ValidationError):
        cancel(st_invoice, attempt)
    with delivery_execution_lock(attempt.pk) as acquired:
        assert acquired and not connection.in_atomic_block
        with pytest.raises(ValidationError):
            cancel(st_invoice, attempt)
    attempt.refresh_from_db()
    assert attempt.result == "PREPARED"


def test_replacement_requires_fresh_review_and_preserves_old_snapshot(st_invoice, smtp_config):
    old_review = review_st_delivery(st_invoice, recipients_to=["old@example.com"])
    attempt = prepare_st_attempt(st_invoice, review=old_review)
    cancel(st_invoice, attempt)
    original = DeliveryAttempt.objects.values().get(pk=attempt.pk)
    with pytest.raises(ValidationError):
        prepare_st_attempt(st_invoice, review=old_review)
    with pytest.raises(ValidationError):
        prepare_retry_attempt(attempt)
    with pytest.raises(ValidationError):
        build_pdf_email(attempt, subject="Račun", body="Račun")
    with pytest.raises(ValidationError):
        mark_submission_started(attempt)
    fake = FakeSmtp()
    with pytest.raises(ValidationError):
        send(attempt, smtp_config, fake)
    assert not fake.calls
    review = review_st_delivery(st_invoice, recipients_to=["correct@example.com"], recipients_cc=["new-copy@example.com"])
    assert review["latest_attempt"] == {"id": attempt.pk, "sequence": 1, "result": "CANCELLED"}
    replacement = prepare_st_attempt(st_invoice, review=review)
    assert replacement.pk != attempt.pk and replacement.sequence == 2
    assert replacement.recipients_to == ["correct@example.com"]
    assert replacement.recipients_cc == ["new-copy@example.com"]
    assert replacement.recipients_bcc == ["accountant@example.com"]
    assert replacement.result == "PREPARED" and replacement.submission_started_at is None
    assert DeliveryAttempt.objects.values().get(pk=attempt.pk) == original
    with pytest.raises(ValidationError):
        cancel(st_invoice, attempt)
    with pytest.raises(ValidationError):
        prepare_st_attempt(st_invoice, review=review)
    cancel(st_invoice, replacement)
    with pytest.raises(ValidationError):
        prepare_st_attempt(st_invoice, review=review)
    third = prepare(st_invoice)
    assert third.sequence == 3 and send(third, smtp_config).result == "ACCEPTED"


def _worker(operation):
    close_old_connections()
    try:
        return operation()
    finally:
        connection.close()


@pytest.mark.parametrize("competitor", ["send", "cancel"])
def test_cancellation_lock_winner_excludes_competitor(st_invoice, smtp_config, competitor):
    attempt = prepare(st_invoice)
    active, release = Event(), Event()
    original_save = AuditLogEntry.save

    def audit_save(entry, *args, **kwargs):
        assert connection.in_atomic_block
        active.set()
        assert release.wait(timeout=10)
        return original_save(entry, *args, **kwargs)

    fake = FakeSmtp()
    with patch.object(AuditLogEntry, "save", audit_save), ThreadPoolExecutor(max_workers=1) as pool:
        winner = pool.submit(_worker, lambda: cancel(st_invoice, attempt))
        try:
            assert active.wait(timeout=10)
            with pytest.raises(ValidationError):
                send(attempt, smtp_config, fake) if competitor == "send" else cancel(st_invoice, attempt)
        finally:
            release.set()
        assert winner.result(timeout=10).result == "CANCELLED"
    assert not fake.calls and AuditLogEntry.objects.count() == 1


def test_active_send_wins_over_cancellation(st_invoice, smtp_config):
    attempt = prepare(st_invoice)
    active, release = Event(), Event()

    def data(payload):
        active.set()
        assert release.wait(timeout=10)
        return 250, b"queued"

    fake = FakeSmtp({"data": data})
    with ThreadPoolExecutor(max_workers=1) as pool:
        winner = pool.submit(_worker, lambda: send(attempt, smtp_config, fake))
        try:
            assert active.wait(timeout=10)
            with pytest.raises(ValidationError):
                cancel(st_invoice, attempt)
            assert recover_interrupted_submissions(invoice_ids=[st_invoice.pk]) == 0
        finally:
            release.set()
        assert winner.result(timeout=10).result == "ACCEPTED"
    assert sum(call[0] == "data" for call in fake.calls) == 1
    assert not AuditLogEntry.objects.exists()


def test_simultaneous_replacement_preparations_have_one_winner(st_invoice):
    cancel(st_invoice, prepare(st_invoice))
    review = review_st_delivery(st_invoice, recipients_to=["correct@example.com"])
    barrier = Barrier(2)

    def prepare_once():
        barrier.wait(timeout=10)
        try:
            prepare_st_attempt(st_invoice, review=review)
            return 1
        except ValidationError:
            return 0

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(lambda _: _worker(prepare_once), range(2))) == 1
    assert list(DeliveryAttempt.objects.values_list("sequence", "result")) == [(1, "CANCELLED"), (2, "PREPARED")]


def test_cancelled_choice_migration_preserves_existing_rows(isolated_migration_db):
    executor = MigrationExecutor(connection)
    targets = executor.loader.graph.leaf_nodes()
    old_targets = [(app, "0006_deliveryattempt_execution_id") if app == "delivery" else (app, name) for app, name in targets]
    executor.migrate(old_targets)
    company = Company.objects.create(
        naziv="Test izdajatelj", davcna_stevilka="15012557", maticna_stevilka="1234567000",
        naslov="Test 1", trr="SI56191000000123438", logotip="logo.png",
        email_racunovodja="accountant@example.com", email_posiljatelj="sender@example.com",
        keychain_kljuc_posiljatelja="synthetic-reference",
    )
    invoice = Invoice.objects.create(
        company=company, document_type="ST", stevilka="ST-2026-0001",
        datum_izdaje=date(2026, 9, 1), obdobje_od=date(2026, 8, 1),
        obdobje_do=date(2026, 8, 31), status="IZBRAN_ZA_POSILJANJE",
    )
    attempt = DeliveryAttempt.objects.create(
        invoice=invoice, sequence=1, sender="sender@example.com",
        recipients_to=["buyer@example.com"], recipients_bcc=["accountant@example.com"],
        pdf_path="synthetic/official.pdf", pdf_checksum="a" * 64,
    )
    before = DeliveryAttempt.objects.values().get(pk=attempt.pk)
    executor = MigrationExecutor(connection)
    with connection.execute_wrapper(_reject_delivery_writes):
        executor.migrate(targets)
    state = executor.loader.project_state(targets).apps.get_model("delivery", "DeliveryAttempt")
    assert "CANCELLED" in dict(state._meta.get_field("result").choices)
    assert DeliveryAttempt.objects.values().get(pk=attempt.pk) == before
    cancel(invoice, attempt).full_clean()
    with pytest.raises(IrreversibleError):
        MigrationExecutor(connection).migrate(old_targets)
    attempt.refresh_from_db()
    assert attempt.result == "CANCELLED"


def _reject_delivery_writes(execute, sql, params, many, context):
    if "delivery_deliveryattempt" in sql.lower():
        assert sql.lstrip().upper().startswith("SELECT"), sql
    return execute(sql, params, many, context)
