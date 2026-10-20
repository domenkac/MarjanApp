"""Synthetic report delivery recovery; never use SMTP or credential stores."""

# ruff: noqa: F401, F811
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from queue import Queue
from threading import Barrier, Thread

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection
from django.urls import reverse
from django.utils import timezone

from core.models import AuditLogEntry
from delivery import report_recovery as recovery
from delivery.models import AccountantReportDeliveryAttempt, MonthlySummaryAttempt
from delivery.services import prepare_monthly_summary
from tests.accounting_export.test_workflow import (
    AS_OF,
    PERIOD,
    Smtp,
    company,
    prepared,
    run,
    setup,
)
from tests.delivery.test_monthly_summary import (
    _attempt,
    _step_6,
    configuration,
    ready_company,
)

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(params=["monthly", "accountant"])
def case(request, settings, tmp_path):
    if request.param == "monthly":
        data = request.getfixturevalue("ready_company")
        config = request.getfixturevalue("configuration")
        invoice = _step_6(data, settings, tmp_path)
        _attempt(invoice, "ACCEPTED")
        attempt = prepare_monthly_summary(data["cycle"], config)
        scope = {"cycle_id": attempt.cycle_id}
        return attempt, scope, recovery.recover_monthly_summary_submissions, recovery.resolve_monthly_summary_as_sent, recovery.resolve_monthly_summary_not_sent
    owner = request.getfixturevalue("setup")
    attempt = prepared(owner, tmp_path)
    scope = {"company_id": owner.pk, "period": PERIOD}
    return attempt, scope, recovery.recover_accountant_report_submissions, recovery.resolve_accountant_report_as_sent, recovery.resolve_accountant_report_not_sent


def unknown(attempt):
    type(attempt).objects.filter(pk=attempt.pk).update(result="UNKNOWN", completed_at=timezone.now())
    attempt.refresh_from_db()
    return attempt


def audits(attempt):
    return AuditLogEntry.objects.filter(
        content_type=ContentType.objects.get_for_model(type(attempt)), object_id=attempt.pk,
    )


def test_recovery_is_audited_repeat_safe_and_respects_active_owner(case):
    attempt, scope, recover, _, _ = case
    type(attempt).objects.filter(pk=attempt.pk).update(result="SUBMISSION_STARTED")
    before = type(attempt).objects.values().get(pk=attempt.pk)
    with recovery.report_execution_lock(attempt) as acquired:
        assert acquired
        assert recover(**scope) == 0
    assert recover(**scope) == 1
    assert recover(**scope) == 0
    after = type(attempt).objects.values().get(pk=attempt.pk)
    for field in ("result", "result_code", "message", "completed_at"):
        before.pop(field)
        after.pop(field)
    assert before == after
    attempt.refresh_from_db()
    assert attempt.result == "UNKNOWN"
    assert audits(attempt).get().nova_vrednost == "UNKNOWN"


def test_sent_preserves_unknown_and_rejects_second_or_opposite_decision(case, tmp_path):
    attempt, scope, _, sent, not_sent = case
    unknown(attempt)
    before = type(attempt).objects.values().get(pk=attempt.pk)
    sent(attempt, reason="Verified in provider sent folder", **scope)
    assert type(attempt).objects.values().get(pk=attempt.pk) == before
    assert audits(attempt).get().nova_vrednost == "SENT_VERIFIED"
    with pytest.raises(ValidationError):
        sent(attempt, reason="Repeated", **scope)
    with pytest.raises(ValidationError):
        not_sent(attempt, reason="Opposite", local_root=tmp_path, **scope)
    assert type(attempt).objects.count() == 1


def test_not_sent_prepares_one_frozen_retry_without_submission(case, tmp_path):
    attempt, scope, _, sent, not_sent = case
    unknown(attempt)
    before = deepcopy(type(attempt).objects.values().get(pk=attempt.pk))
    retry = not_sent(attempt, reason="Provider confirms no submission", local_root=tmp_path, **scope)
    assert retry.result == "PREPARED" and retry.sequence == attempt.sequence + 1
    for field in ("sender", "recipient", "subject", "body"):
        assert getattr(retry, field) == before[field]
    assert type(attempt).objects.values().get(pk=attempt.pk) == before
    assert audits(attempt).get().nova_vrednost == "NOT_SENT_CONFIRMED"
    with pytest.raises(ValidationError):
        not_sent(attempt, reason="Repeated", local_root=tmp_path, **scope)
    with pytest.raises(ValidationError):
        sent(attempt, reason="Opposite", **scope)
    assert type(attempt).objects.count() == 2


@pytest.mark.parametrize("decision", ["sent", "not_sent"])
def test_audit_failure_rolls_back_decision_and_retry(case, tmp_path, monkeypatch, decision):
    attempt, scope, _, sent, not_sent = case
    unknown(attempt)
    def fail(*args, **kwargs):
        raise ValidationError("Synthetic audit insertion failure")
    monkeypatch.setattr(AuditLogEntry, "save", fail)
    with pytest.raises(ValidationError):
        if decision == "sent":
            sent(attempt, reason="Checked", **scope)
        else:
            not_sent(attempt, reason="Checked", local_root=tmp_path, **scope)
    assert not audits(attempt).exists()
    assert type(attempt).objects.count() == 1


def test_reason_and_scope_required(case):
    attempt, scope, _, sent, _ = case
    unknown(attempt)
    with pytest.raises(ValidationError):
        sent(attempt, reason="   ", **scope)
    wrong = {**scope}
    key = "cycle_id" if "cycle_id" in scope else "company_id"
    wrong[key] += 1000
    with pytest.raises(ValidationError):
        sent(attempt, reason="Checked", **wrong)
    assert not audits(attempt).exists()


def send(attempt, calls, smtp_factory=None):
    from delivery.services import send_accountant_report, send_monthly_summary
    from delivery.transport import SmtpConfig, SmtpSecurity

    operation = send_monthly_summary if isinstance(attempt, MonthlySummaryAttempt) else send_accountant_report
    config = SmtpConfig(host="smtp.example.com", port=587, security=SmtpSecurity.STARTTLS,
                        timeout_seconds=10, username=attempt.sender)
    return operation(attempt, config, object(), password_loader=lambda _: "test-password",
                     smtp_factory=smtp_factory or (lambda *_: Smtp(calls)))


def test_active_sender_blocks_recovery_and_duplicate_data(case):
    attempt, scope, recover, _, _ = case
    calls = []

    class Active(Smtp):
        def data(self, payload):
            assert recover(**scope) == 0
            with pytest.raises(ValidationError, match="already being sent"):
                send(attempt, calls)
            return super().data(payload)

    assert send(attempt, calls, lambda *_: Active(calls)).result == "ACCEPTED"
    assert len(calls) == 1
    assert recover(**scope) == 0


@pytest.mark.parametrize("decisions", [("sent", "not_sent"), ("not_sent", "not_sent"), ("sent", "sent")])
def test_competing_decisions_have_one_winner(case, tmp_path, decisions):
    attempt, scope, _, sent, not_sent = case
    unknown(attempt)
    barrier = Barrier(2)

    def decide(operation):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            if operation == "sent":
                sent(attempt, reason="Sent folder checked", **scope)
            else:
                not_sent(attempt, reason="Provider confirmed absent", local_root=tmp_path, **scope)
            return True
        except ValidationError:
            return False
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(decide, decisions)) == 1
    assert audits(attempt).filter(polje="unknown_resolution").count() == 1
    assert type(attempt).objects.count() in (1, 2)


@pytest.mark.parametrize("decision", ["sent", "not_sent", "recover"])
def test_operational_ui_post_redirect_get_and_no_get_mutations(case, client, tmp_path, monkeypatch, decision):
    monkeypatch.setattr("ui.views.initial_setup_completed", lambda: True)
    attempt, scope, _, _, _ = case
    if decision == "recover":
        type(attempt).objects.filter(pk=attempt.pk).update(result="SUBMISSION_STARTED")
    else:
        unknown(attempt)
    monthly = isinstance(attempt, MonthlySummaryAttempt)
    endpoint = reverse("ui:monthly_summary_recovery" if monthly else "ui:accountant_report_recovery")
    data = {"attempt_id": attempt.pk, "decision": decision, "reason": "Verified provider records"}
    if monthly:
        data.update(cycle_id=attempt.cycle_id)
    else:
        data.update(company_id=scope["company_id"], report_id=attempt.report_id, year=PERIOD.year, month=PERIOD.month)
    before = list(type(attempt).objects.values())
    assert client.get(endpoint, data).status_code == 405
    assert list(type(attempt).objects.values()) == before
    response = client.post(endpoint, data)
    assert response.status_code == 302
    assert "error=" not in response.url and "ACTION_FAILED" not in response.url
    count = type(attempt).objects.count()
    client.post(endpoint, data)
    assert type(attempt).objects.count() == count
    assert client.get(reverse("ui:mesecni_obracun" if monthly else "ui:dashboard")).status_code == 200


def test_accountant_sent_verification_completes_task_and_stale_report_can_be_replaced(setup, tmp_path):
    from accounting_export.task_state import accountant_control_task_state
    from tests.accounting_export.test_report_snapshot import _invoice, _line

    attempt = prepared(setup, tmp_path)
    unknown(attempt)
    recovery.resolve_accountant_report_as_sent(attempt, company_id=setup.pk, period=PERIOD, reason="Sent folder checked")
    assert accountant_control_task_state(as_of=AS_OF).state == "ZAKLJUCENO"
    calls = []
    assert run(setup, calls).code == "CURRENT"
    assert not calls
    _line(_invoice(setup, number="ST-00002", kind="ST"))
    assert run(setup, calls).code == "ACCEPTED"
    assert len(calls) == 1


CHILD_SEND = r"""
import os, sys
from pathlib import Path
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'app.settings')
import django
django.setup()
from django.conf import settings
from django.db import connection
connection.settings_dict['NAME'] = sys.argv[1]
settings.RENDERING_ROOT = Path(sys.argv[2])
settings.MEDIA_ROOT = Path(sys.argv[3])
from delivery import services
from delivery.models import MonthlySummaryAttempt, AccountantReportDeliveryAttempt
from tests.delivery.test_report_recovery import send
from tests.accounting_export.test_workflow import Smtp
model = MonthlySummaryAttempt if sys.argv[4] == 'monthly' else AccountantReportDeliveryAttempt
mode = sys.argv[6]
transition_name = '_transition_monthly_summary' if sys.argv[4] == 'monthly' else '_transition_accountant_delivery'
original = getattr(services, transition_name)
def transition(*args, **kwargs):
    if mode == 'after_acceptance' and kwargs.get('result') == 'ACCEPTED':
        os._exit(23)
    return original(*args, **kwargs)
setattr(services, transition_name, transition)
class CrashSmtp(Smtp):
    def rcpt(self, recipient):
        if mode == 'before_data':
            os._exit(23)
        return 250, b'ok'
    def data(self, payload):
        with open(sys.argv[7], 'ab') as evidence:
            evidence.write(b'DATA\n')
            evidence.flush()
            os.fsync(evidence.fileno())
        if mode == 'active':
            print('ACTIVE', flush=True)
            sys.stdin.readline()
        elif mode == 'during_data':
            os._exit(23)
        return 250, b'accepted'
send(model.objects.get(pk=sys.argv[5]), [], lambda *_: CrashSmtp([]))
"""


@pytest.mark.parametrize("mode", ["before_data", "during_data", "after_acceptance", "active"])
def test_isolated_process_crash_and_restart(case, settings, tmp_path, mode):
    attempt, scope, recover, _, _ = case
    evidence = tmp_path / "data-count.txt"
    args = [sys.executable, "-c", CHILD_SEND, str(connection.settings_dict["NAME"]),
            str(settings.RENDERING_ROOT), str(settings.MEDIA_ROOT),
            "monthly" if isinstance(attempt, MonthlySummaryAttempt) else "accountant",
            str(attempt.pk), mode, str(evidence)]
    if mode == "active":
        child = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        try:
            output = Queue()
            Thread(target=lambda: output.put(child.stdout.readline()), daemon=True).start()
            assert output.get(timeout=20).strip() == "ACTIVE"
            assert recover(**scope) == 0
            with pytest.raises(ValidationError):
                send(attempt, [])
            child.kill()
            child.communicate(timeout=20)
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=20)
    else:
        child = subprocess.run(args, capture_output=True, text=True, timeout=25, check=False)
        assert child.returncode == 23, child.stderr
    assert (evidence.read_bytes() if evidence.exists() else b"") == (b"" if mode == "before_data" else b"DATA\n")
    assert recover(**scope) == (0 if mode == "before_data" else 1)
    connection.close()
    assert recover(**scope) == 0
    attempt.refresh_from_db()
    assert attempt.result == ("PREPARED" if mode == "before_data" else "UNKNOWN")


def test_recovery_audit_failure_rolls_back(case, monkeypatch):
    attempt, scope, recover, _, _ = case
    type(attempt).objects.filter(pk=attempt.pk).update(result="SUBMISSION_STARTED")
    def fail(*args, **kwargs):
        raise ValidationError("Audit unavailable")
    monkeypatch.setattr(AuditLogEntry, "save", fail)
    with pytest.raises(ValidationError):
        recover(**scope)
    attempt.refresh_from_db()
    assert attempt.result == "SUBMISSION_STARTED"
    assert not audits(attempt).exists()


def test_prepared_retry_requires_later_send_and_one_data(case, tmp_path):
    attempt, scope, _, _, not_sent = case
    unknown(attempt)
    retry = not_sent(attempt, reason="Checked provider logs", local_root=tmp_path, **scope)
    calls = []
    assert retry.result == "PREPARED"
    assert send(retry, calls).result == "ACCEPTED"
    with pytest.raises(ValidationError):
        send(retry, calls)
    assert len(calls) == 1


@pytest.mark.parametrize("state", ["ACCEPTED", "CONFIRMED_FAILURE", "PREPARED"])
def test_non_unknown_cannot_be_manually_resolved(case, state):
    attempt, scope, _, sent, _ = case
    type(attempt).objects.filter(pk=attempt.pk).update(result=state)
    with pytest.raises(ValidationError):
        sent(attempt, reason="Checked", **scope)
    assert not audits(attempt).exists()


def test_frozen_content_changed_during_credentials_blocks_data(case):
    attempt, _, _, _, _ = case
    calls = []
    from delivery.services import send_accountant_report, send_monthly_summary
    from delivery.transport import SmtpConfig, SmtpSecurity
    operation = send_monthly_summary if isinstance(attempt, MonthlySummaryAttempt) else send_accountant_report
    def password(_):
        type(attempt).objects.filter(pk=attempt.pk).update(body="tampered body")
        return "test-password"
    config = SmtpConfig(host="smtp.example.com", port=587, security=SmtpSecurity.STARTTLS,
                        timeout_seconds=10, username=attempt.sender)
    result = operation(attempt, config, object(), password_loader=password, smtp_factory=lambda *_: Smtp(calls))
    assert result.result == "CONFIRMED_FAILURE"
    assert not calls


@pytest.mark.parametrize("damage", ["pdf", "checksum", "source"])
def test_accountant_retry_rejects_damaged_or_stale_evidence(setup, tmp_path, damage):
    from accounting_export.models import AccountantReportPdfArtifact
    from tests.accounting_export.test_report_snapshot import _invoice, _line
    attempt = prepared(setup, tmp_path)
    unknown(attempt)
    if damage == "pdf":
        from pathlib import Path
        artifact = AccountantReportPdfArtifact.objects.get(report_id=attempt.report_id)
        Path(artifact.local_path).write_bytes(b"tampered PDF")
    elif damage == "checksum":
        AccountantReportDeliveryAttempt.objects.filter(pk=attempt.pk).update(attachment_checksum="0" * 64)
        attempt.refresh_from_db()
    else:
        _line(_invoice(setup, number="ST-00002", kind="ST"))
    with pytest.raises(ValidationError):
        recovery.resolve_accountant_report_not_sent(attempt, company_id=setup.pk, period=PERIOD,
                                                    local_root=tmp_path, reason="Checked")
    assert not audits(attempt).exists()
    assert AccountantReportDeliveryAttempt.objects.count() == 1


def test_concurrent_prepared_retry_has_one_data_owner(case, tmp_path):
    attempt, scope, _, _, not_sent = case
    unknown(attempt)
    retry = not_sent(attempt, reason="Provider confirmed absent", local_root=tmp_path, **scope)
    barrier = Barrier(2)
    calls = []
    def submit(_):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return send(retry, calls).result
        except ValidationError:
            return "BLOCKED"
        finally:
            close_old_connections()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, range(2)))
    assert results.count("ACCEPTED") == 1
    assert len(calls) == 1
    assert type(attempt).objects.count() == 2


def test_accountant_five_issuer_recovery_and_decisions_stay_isolated(setup, tmp_path):
    from companies.models import Company
    from tests.accounting_export.test_report_snapshot import _invoice, _line
    attempts = [prepared(setup, tmp_path)]
    for index in range(4):
        other = Company.objects.create(naziv=f"Issuer {index}", davcna_stevilka=f"8765432{index}",
                                       email_posiljatelj=f"issuer{index}@example.com",
                                       email_racunovodja=f"accountant{index}@example.com")
        _line(_invoice(other))
        attempts.append(prepared(other, tmp_path))
    AccountantReportDeliveryAttempt.objects.filter(pk__in=[item.pk for item in attempts]).update(result="SUBMISSION_STARTED")
    before = list(AccountantReportDeliveryAttempt.objects.exclude(pk=attempts[0].pk).values())
    assert recovery.recover_accountant_report_submissions(company_id=setup.pk, period=PERIOD) == 1
    attempts[0].refresh_from_db()
    recovery.resolve_accountant_report_as_sent(attempts[0], company_id=setup.pk, period=PERIOD, reason="Exact recipient and CSV verified")
    assert list(AccountantReportDeliveryAttempt.objects.exclude(pk=attempts[0].pk).values()) == before


@pytest.mark.parametrize("field", ["company_id", "report_id", "month", "attempt_id"])
def test_foreign_or_stale_accountant_post_is_rejected(setup, tmp_path, client, field):
    attempt = prepared(setup, tmp_path)
    unknown(attempt)
    data = {"company_id": setup.pk, "report_id": attempt.report_id, "year": PERIOD.year,
            "month": PERIOD.month, "attempt_id": attempt.pk, "decision": "sent", "reason": "Checked"}
    data[field] += 1
    assert "ACTION_FAILED" in client.post(reverse("ui:accountant_report_recovery"), data).url
    assert not audits(attempt).exists()


def test_report_recovery_posts_require_csrf(case):
    from django.test import Client
    attempt, _, _, _, _ = case
    endpoint = reverse("ui:monthly_summary_recovery" if isinstance(attempt, MonthlySummaryAttempt) else "ui:accountant_report_recovery")
    assert Client(enforce_csrf_checks=True).post(endpoint, {}).status_code == 403
