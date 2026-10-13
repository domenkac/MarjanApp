from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from importlib import reload
from pathlib import Path

import pytest
from django.db import connection
from django.utils import timezone

from accounting_export.models import AccountantReport
from accounting_export.services import create_accountant_report
from accounting_export.task_state import (
    accountant_control_dashboard_tasks,
    accountant_control_task_state,
)
from companies.models import Company
from delivery.models import AccountantReportDeliveryAttempt as Attempt
from invoicing.models import Invoice, MonthlyBatch, MonthlyCycle
from tests.accounting_export.test_report_snapshot import (
    _ensure_completed_batch,
    _invoice,
    _lease,
)
from tests.accounting_export.test_report_snapshot import (
    company as company,  # noqa: PLC0414
)

pytestmark = pytest.mark.django_db
AS_OF = date(2026, 10, 1)


def accept(report, *, result="ACCEPTED", completed_at=None, sequence=1):
    return Attempt.objects.create(
        report=report, sequence=sequence, result=result,
        completed_at=completed_at or datetime(2026, 10, 1, 8, tzinfo=UTC),
        sender="sender@example.com", recipient="accountant@example.com",
        subject="Report", body="Report", attachment_filename="report.csv",
        attachment_checksum="a" * 64,
    )


@pytest.fixture
def reported(company):
    _invoice(company)
    report = create_accountant_report(company=company, period=date(2026, 9, 1))
    return report


def state(**kwargs):
    return accountant_control_task_state(as_of=AS_OF, **kwargs)


@pytest.mark.parametrize("as_of,period", [
    (date(2026, 10, 1), date(2026, 9, 1)),
    (date(2026, 10, 31), date(2026, 9, 1)),
    (date(2027, 1, 3), date(2026, 12, 1)),
    (datetime(2026, 9, 30, 22, 30, tzinfo=UTC), date(2026, 9, 1)),
])
def test_previous_local_month(as_of, period):
    with timezone.override("Europe/Ljubljana"):
        assert accountant_control_task_state(as_of=as_of).period == period


def test_default_uses_django_local_time(monkeypatch):
    monkeypatch.setattr(timezone, "now", lambda: datetime(2026, 9, 30, 22, 30, tzinfo=UTC))
    with timezone.override("Europe/Ljubljana"):
        assert accountant_control_task_state().period == date(2026, 9, 1)


@pytest.mark.parametrize("invalid", [True, False, 0, 32, -1, 5.0, "5", None])
def test_invalid_deadline(invalid):
    with pytest.raises(ValueError):
        state(deadline_day=invalid)


def test_nonexistent_day_and_naive_datetime_rejected():
    with pytest.raises(ValueError):
        accountant_control_task_state(as_of=date(2026, 2, 1), deadline_day=31)
    with pytest.raises(ValueError):
        accountant_control_task_state(as_of=datetime(2026, 10, 1))  # noqa: DTZ001


@pytest.mark.parametrize("as_of,day,effective", [
    (AS_OF, 5, date(2026, 10, 5)),
    (AS_OF, 8, date(2026, 10, 8)),
    (date(2026, 9, 1), 5, date(2026, 9, 7)),
    (date(2026, 7, 1), 5, date(2026, 7, 6)),
    (date(2026, 6, 1), 25, date(2026, 6, 26)),
    (date(2026, 4, 1), 6, date(2026, 4, 7)),
    (date(2026, 4, 1), 5, date(2026, 4, 7)),
    (date(2026, 5, 1), 1, date(2026, 5, 4)),
    (date(2026, 12, 1), 25, date(2026, 12, 28)),
])
def test_deadline_calendar(as_of, day, effective):
    result = accountant_control_task_state(as_of=as_of, deadline_day=day)
    assert result.deadline == as_of.replace(day=day)
    assert result.effective_deadline == effective


@pytest.mark.parametrize("as_of,expected", [
    (date(2026, 10, 5), "ODPRTO"), (date(2026, 10, 6), "OPOZORILO"),
    (date(2026, 5, 4), "ODPRTO"), (date(2026, 5, 5), "OPOZORILO"),
])
def test_deadline_boundary(as_of, expected):
    assert accountant_control_task_state(as_of=as_of, deadline_day=5 if as_of.month == 10 else 1).state == expected


def test_membership_is_historical(company):
    _ensure_completed_batch(company)
    Company.objects.filter(pk=company.pk).update(aktivno=False, naziv="New name")
    Company.objects.create(naziv="New active company", davcna_stevilka="87654321")
    result = state()
    assert result.ready
    assert [(c.company_id, c.label) for c in result.companies] == [(company.pk, "Zgodovinska Alfa")]
    assert result.state == "ODPRTO"


@pytest.mark.parametrize("evidence", ["missing", "unfinished", "no_batches", "uncalculated"])
def test_missing_evidence_never_completes(company, evidence):
    if evidence != "missing":
        batch = _ensure_completed_batch(company)
        if evidence == "unfinished":
            MonthlyCycle.objects.filter(pk=batch.cycle_id).update(status="V_TEKU")
        elif evidence == "no_batches":
            batch.delete()
        else:
            MonthlyBatch.objects.filter(pk=batch.pk).update(calculation_status="NOT_CALCULATED")
    result = state()
    assert not result.ready
    assert result.issue_code
    assert result.state == "ODPRTO" and result.active


@pytest.mark.parametrize("result,diagnostic", [
    ("ACCEPTED", "CURRENT"), ("PREPARED", "PREPARED"),
    ("SUBMISSION_STARTED", "PENDING"), ("CONFIRMED_FAILURE", "FAILED"), ("UNKNOWN", "UNKNOWN"),
])
def test_delivery_evidence(reported, result, diagnostic):
    attempt = accept(reported, result=result)
    task = state()
    row = task.companies[0]
    assert row.state == diagnostic
    assert row.latest_attempt_id == attempt.pk and row.latest_attempt_result == result
    assert row.accepted_report_id == (reported.pk if result == "ACCEPTED" else None)
    assert task.state == ("ZAKLJUCENO" if result == "ACCEPTED" else "ODPRTO")
    assert task.active == (result != "ACCEPTED")


def test_no_attempt_is_not_sent(reported):
    row = state().companies[0]
    assert row.state == "NOT_SENT" and row.latest_attempt_id is None


def test_failed_retry_then_accepted(reported):
    accept(reported, result="CONFIRMED_FAILURE")
    accepted = accept(reported, sequence=2)
    assert state().companies[0].latest_attempt_id == accepted.pk
    assert state().state == "ZAKLJUCENO"


@pytest.mark.parametrize("kind", ["NP", "ST", "STORNO", "DOBROPIS"])
def test_new_numbered_document_stales_and_replacement_clears(reported, kind):
    accept(reported)
    frozen = deepcopy(reported.snapshot)
    assert state().state == "ZAKLJUCENO"
    _invoice(reported.company, number=f"{kind}-00002", kind=kind)
    stale = accountant_control_task_state(as_of=date(2026, 10, 20))
    assert stale.state == "ZASTARELO" and stale.warning
    assert stale.companies[0].stale and stale.companies[0].issue_code == "NEW_ISSUED_DOCUMENT"
    replacement = create_accountant_report(company=reported.company, period=reported.period)
    accept(replacement, result="UNKNOWN")
    assert state().state == "ZASTARELO"
    accept(replacement, sequence=2, completed_at=datetime(2026, 10, 2, tzinfo=UTC))
    assert accountant_control_task_state(as_of=date(2026, 10, 20)).state == "ZAKLJUCENO"
    assert state().companies[0].accepted_report_id == replacement.pk
    reported.refresh_from_db()
    assert reported.snapshot == frozen


def test_latest_acceptance_uses_completion_not_report_generation(reported):
    later = create_accountant_report(company=reported.company, period=reported.period)
    accept(later)
    accepted = accept(reported, completed_at=datetime(2026, 10, 3, tzinfo=UTC))
    assert state().companies[0].accepted_report_id == reported.pk
    assert state().companies[0].latest_attempt_id == accepted.pk


def test_tied_acceptance_uses_stable_attempt_order(reported):
    accept(reported)
    later = create_accountant_report(company=reported.company, period=reported.period)
    accept(later)
    assert state().companies[0].accepted_report_id == later.pk


def test_unrelated_month_company_and_live_edits_do_not_stale(reported):
    accept(reported)
    _invoice(reported.company, number="NP-00002", issued=date(2026, 10, 2))
    other = Company.objects.create(naziv="Other", davcna_stevilka="87654321")
    _invoice(other, number="NP-00002", completed_evidence=False)
    lease = _lease(reported.company)
    lease.oznaka_pogodbe = "Edited contract"
    lease.save()
    lease.tenant.naziv = "Edited tenant"
    lease.tenant.save()
    Company.objects.filter(pk=reported.company_id).update(naziv="Edited", ddv_zavezanec=False)
    assert state().state == "ZAKLJUCENO"


@pytest.mark.parametrize("status,number", [
    ("OSNUTEK", "NP-00002"), ("ZA_PONOVNI_PREGLED", "NP-00002"),
    ("ARHIVIRAN_NI_IZDAN", "NP-00002"), ("POSLAN", ""), ("POSLAN", None),
])
def test_authoritative_non_issued_exclusions(reported, status, number):
    accept(reported)
    _invoice(reported.company, number=number, status=status)
    assert state().state == "ZAKLJUCENO"


def test_partial_overdue_and_complete_precedence(reported):
    accept(reported)
    other = Company.objects.create(naziv="Other", davcna_stevilka="87654321")
    _invoice(other)
    assert state().state == "DELNO"
    assert accountant_control_task_state(as_of=date(2026, 10, 6)).state == "OPOZORILO"
    accept(create_accountant_report(company=other, period=reported.period))
    assert accountant_control_task_state(as_of=date(2026, 10, 6)).state == "ZAKLJUCENO"


def read_only_execute(execute, sql, params, many, context):
    assert sql.lstrip().split()[0].upper() == "SELECT", sql
    return execute(sql, params, many, context)


def forbid_side_effects(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Read-only task invoked an artifact/delivery operation")

    for target in (
        "accounting_export.services.create_accountant_report",
        "accounting_export.csv_renderer.render_accountant_report_csv",
        "accounting_export.pdf_renderer.render_accountant_report_pdf",
        "accounting_export.archive.verify_accountant_report_pdf_artifact",
        "delivery.services.prepare_accountant_report_delivery",
        "delivery.services.send_accountant_report",
        "delivery.transport.send_prepared_email",
        "delivery.credentials.load_password",
        "keyring.get_password",
    ):
        monkeypatch.setattr(target, forbidden)
    for name in ("read_bytes", "write_bytes", "write_text"):
        monkeypatch.setattr(Path, name, forbidden)


def test_read_only_and_no_monthly_cycle_coupling(reported, monkeypatch):
    accept(reported)
    batch = MonthlyBatch.objects.get(company=reported.company, cycle__obdobje=reported.period)
    MonthlyCycle.objects.filter(pk=batch.cycle_id).update(completion_snapshot={"sentinel": "unchanged"})
    before = {
        model: list(model.objects.values())
        for model in (Invoice, MonthlyCycle, MonthlyBatch, AccountantReport, Attempt)
    }
    forbid_side_effects(monkeypatch)
    monkeypatch.setattr(Path, "open", lambda *a, **k: pytest.fail("Filesystem access"))
    with connection.execute_wrapper(read_only_execute):
        assert state().state == "ZAKLJUCENO"
        _ = accountant_control_task_state(as_of=AS_OF + timedelta(days=20))
    assert before == {model: list(model.objects.values()) for model in before}


def test_query_count_does_not_grow_per_company(reported, django_assert_num_queries):
    accept(reported)
    with django_assert_num_queries(4):
        state()
    for index in range(4):
        company = Company.objects.create(naziv=f"Other {index}", davcna_stevilka=f"8765432{index}")
        _invoice(company)
        accept(create_accountant_report(company=company, period=reported.period))
    with django_assert_num_queries(4):
        assert state().state == "ZAKLJUCENO"


def completed_period(company, period):
    batch = _ensure_completed_batch(company, period)
    snapshot = deepcopy(batch.calculation_snapshot)
    snapshot["period_start"] = period.isoformat()
    MonthlyBatch.objects.filter(pk=batch.pk).update(calculation_snapshot=snapshot)
    return batch


def test_dashboard_previous_month_without_invented_history():
    tasks = accountant_control_dashboard_tasks(as_of=AS_OF)
    assert [(task.period, task.state) for task in tasks] == [(date(2026, 9, 1), "ODPRTO")]
    assert not tasks[0].ready


def test_historical_accepted_storno_reactivation_and_replacement(reported):
    accept(reported)
    november = date(2026, 11, 1)
    tasks = accountant_control_dashboard_tasks(as_of=november)
    assert [task.period for task in tasks] == [date(2026, 10, 1)]
    _invoice(reported.company, number="STORNO-00002", kind="STORNO")
    tasks = accountant_control_dashboard_tasks(as_of=november)
    assert [(task.period, task.state) for task in tasks] == [
        (date(2026, 9, 1), "ZASTARELO"), (date(2026, 10, 1), "ODPRTO"),
    ]
    assert tasks[0].deadline == date(2026, 10, 5)
    assert tasks[0].effective_deadline == date(2026, 10, 5)
    frozen = deepcopy(reported.snapshot)
    replacement = create_accountant_report(company=reported.company, period=reported.period)
    accept(replacement, completed_at=datetime(2026, 11, 1, 10, tzinfo=UTC))
    assert [task.period for task in accountant_control_dashboard_tasks(as_of=november)] == [date(2026, 10, 1)]
    reported.refresh_from_db()
    assert reported.snapshot == frozen


def test_old_unsent_and_unfinished_periods_remain_outstanding(company):
    completed_period(company, date(2026, 8, 1))
    completed_period(company, date(2026, 9, 1))
    MonthlyCycle.objects.create(obdobje=date(2026, 10, 1), datum_izdaje=date(2026, 10, 5))
    tasks = accountant_control_dashboard_tasks(as_of=date(2026, 11, 1))
    assert [(task.period, task.state) for task in tasks] == [
        (date(2026, 8, 1), "OPOZORILO"),
        (date(2026, 9, 1), "OPOZORILO"),
        (date(2026, 10, 1), "ODPRTO"),
    ]
    assert tasks[0].deadline == date(2026, 9, 5)
    assert tasks[0].effective_deadline == date(2026, 9, 7)
    assert tasks[1].deadline == date(2026, 10, 5)
    assert not tasks[2].ready


def test_dashboard_ignores_current_and_future_cycle_periods(company):
    completed_period(company, date(2026, 11, 1))
    completed_period(company, date(2026, 12, 1))
    assert [task.period for task in accountant_control_dashboard_tasks(as_of=date(2026, 11, 1))] == [date(2026, 10, 1)]


def test_dashboard_query_count_constant_across_periods_and_companies(reported, django_assert_num_queries):
    accept(reported)
    with django_assert_num_queries(4):
        assert accountant_control_dashboard_tasks(as_of=date(2026, 11, 1))
    for month in (5, 7, 8, 10):
        period = date(2026, month, 1)
        completed_period(reported.company, period)
        _invoice(reported.company, number=f"NP-{month:05}", issued=period)
    other = Company.objects.create(naziv="Other", davcna_stevilka="87654321")
    completed_period(other, date(2026, 7, 1))
    with django_assert_num_queries(4):
        tasks = accountant_control_dashboard_tasks(as_of=date(2026, 11, 1))
    assert [task.period.month for task in tasks] == [5, 7, 8, 10]
    assert len(tasks[1].companies) == 2


def test_schema3_reader_independent_of_current_writer(company, monkeypatch):
    from accounting_export import task_state

    batch = _ensure_completed_batch(company)
    snapshot = deepcopy(batch.calculation_snapshot)
    snapshot["schema_version"] = 3
    MonthlyBatch.objects.filter(pk=batch.pk).update(
        calculation_schema_version=3, calculation_snapshot=snapshot,
    )
    monkeypatch.setattr("invoicing.services.CALCULATION_SNAPSHOT_VERSION", 5)
    try:
        reload(task_state)
        assert state().ready
    finally:
        monkeypatch.undo()
        reload(task_state)


@pytest.mark.parametrize("batch_version,snapshot_version", [
    (3, 3), (4, 4), (3, 4), (4, 3), (None, 3),
    (3, True), (3, "3"), (3, None),
    (5, 5),
])
def test_persisted_version_contract(company, batch_version, snapshot_version):
    batch = _ensure_completed_batch(company)
    snapshot = deepcopy(batch.calculation_snapshot)
    snapshot["schema_version"] = snapshot_version
    MonthlyBatch.objects.filter(pk=batch.pk).update(
        calculation_schema_version=batch_version, calculation_snapshot=snapshot,
    )
    task = state()
    assert task.ready == (batch_version == snapshot_version and batch_version in {3, 4, 5})
    if not task.ready:
        assert task.issue_code == "MONTHLY_SNAPSHOT_NOT_READY"
        assert task.state != "ZAKLJUCENO"


def test_batch_metadata_reader_rejects_non_integer_types(company):
    from accounting_export.task_state import _monthly_membership

    batch = _ensure_completed_batch(company)
    evidence = MonthlyBatch.objects.values().get(pk=batch.pk)
    # Integer database columns normalize some inputs; check the reader's type contract directly.
    for invalid in (True, False, "3", None, 3.0):
        membership, issue = _monthly_membership(
            batch.cycle.obdobje, batch.cycle,
            [evidence | {"calculation_schema_version": invalid}],
        )
        assert not membership and issue == "MONTHLY_SNAPSHOT_NOT_READY"


def test_historical_dashboard_is_read_only(reported, monkeypatch):
    accept(reported)
    _invoice(reported.company, number="STORNO-00002", kind="STORNO")
    completed_period(reported.company, date(2026, 8, 1))
    before = {
        model: list(model.objects.values())
        for model in (Invoice, MonthlyCycle, MonthlyBatch, AccountantReport, Attempt)
    }
    forbid_side_effects(monkeypatch)
    monkeypatch.setattr(Path, "open", lambda *a, **k: pytest.fail("Filesystem access"))
    with connection.execute_wrapper(read_only_execute):
        tasks = accountant_control_dashboard_tasks(as_of=date(2026, 11, 1))
    assert [task.state for task in tasks] == ["OPOZORILO", "ZASTARELO", "ODPRTO"]
    assert before == {model: list(model.objects.values()) for model in before}
