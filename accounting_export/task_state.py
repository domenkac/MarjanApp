"""Derived, database-read-only accountant control tasks, including outstanding history."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from django.db import models
from django.utils import timezone

from core.calendar import next_slovenian_working_day
from delivery.models import AccountantReportDeliveryAttempt as Attempt
from invoicing.models import MonthlyBatch, MonthlyCycle

from .services import issued_invoice_queryset

_SUPPORTED_MONTHLY_SNAPSHOT_VERSIONS = frozenset({3, 4})

class TaskState(models.TextChoices):
    ODPRTO = "ODPRTO", "Odprto"
    DELNO = "DELNO", "Delno"
    OPOZORILO = "OPOZORILO", "Opozorilo"
    ZAKLJUCENO = "ZAKLJUCENO", "Zaključeno"
    ZASTARELO = "ZASTARELO", "Zastarelo"


_COMPANY_LABELS = {
    "CURRENT": "Poslano, aktualno",
    "NOT_SENT": "Še ni poslano",
    "STALE": "Zastarelo – manjka nov izdan dokument",
    "PREPARED": "Pripravljeno, še ni poslano",
    "PENDING": "Pošiljanje v teku",
    "FAILED": "Pošiljanje ni uspelo",
    "UNKNOWN": "Rezultat pošiljanja ni znan",
}
_ATTEMPT_STATES = {
    Attempt.Result.PREPARED: "PREPARED",
    Attempt.Result.SUBMISSION_STARTED: "PENDING",
    Attempt.Result.CONFIRMED_FAILURE: "FAILED",
    Attempt.Result.UNKNOWN: "UNKNOWN",
}
_ISSUE_LABELS = {
    "MONTHLY_EVIDENCE_NOT_READY": "Mesečni obračun za poročano obdobje še ni zaključen.",
    "MONTHLY_MEMBERSHIP_MISSING": "Manjkajo shranjeni mesečni paketi za poročano obdobje.",
    "MONTHLY_SNAPSHOT_NOT_READY": "Shranjeni mesečni paketi še nimajo ustreznega zamrznjenega posnetka.",
}


@dataclass(frozen=True)
class CompanyTaskState:
    company_id: int
    label: str
    state: str
    accepted_report_id: int | None
    latest_attempt_id: int | None
    latest_attempt_result: str | None
    stale: bool
    issue_code: str
    action_label: str = ""
    action_blocked_message: str = ""

    @property
    def state_label(self):
        return _COMPANY_LABELS[self.state]


@dataclass(frozen=True)
class AccountantControlTask:
    period: date
    deadline: date
    effective_deadline: date
    state: TaskState
    companies: tuple[CompanyTaskState, ...]
    ready: bool
    issue_code: str

    @property
    def active(self):
        return self.state != TaskState.ZAKLJUCENO

    @property
    def warning(self):
        return self.state in (TaskState.OPOZORILO, TaskState.ZASTARELO)

    @property
    def state_label(self):
        return self.state.label

    @property
    def issue_label(self):
        return _ISSUE_LABELS.get(self.issue_code, "")


def _local_date(as_of):
    if as_of is None:
        return timezone.localdate()
    if isinstance(as_of, datetime):
        if timezone.is_naive(as_of):
            raise ValueError("as_of datetime must be timezone-aware.")
        return timezone.localtime(as_of).date()
    if not isinstance(as_of, date):
        raise TypeError("as_of must be a date or an aware datetime.")
    return as_of


def _monthly_membership(period, cycle, batches):
    if (
        cycle is None or cycle.status != MonthlyCycle.Status.ZAKLJUCEN
        or cycle.trenutni_korak != MonthlyCycle.Korak.ZAKLJUCEK
    ):
        return (), "MONTHLY_EVIDENCE_NOT_READY"
    if not batches:
        return (), "MONTHLY_MEMBERSHIP_MISSING"
    membership = []
    for batch in batches:
        snapshot = batch["calculation_snapshot"]
        company = snapshot.get("company") if isinstance(snapshot, dict) else None
        batch_version = batch["calculation_schema_version"]
        snapshot_version = snapshot.get("schema_version") if isinstance(snapshot, dict) else None
        if (
            batch["calculation_status"] != MonthlyBatch.CalculationStatus.CALCULATED
            or type(batch_version) is not int
            or batch_version not in _SUPPORTED_MONTHLY_SNAPSHOT_VERSIONS
            or type(snapshot_version) is not int
            or snapshot_version != batch_version
            or not isinstance(company, dict)
            or company.get("id") != batch["company_id"]
            or not isinstance(company.get("name"), str)
            or not company["name"].strip()
            or snapshot.get("period_start") != period.isoformat()
        ):
            return (), "MONTHLY_SNAPSHOT_NOT_READY"
        membership.append((batch["company_id"], company["name"]))
    return membership, ""


def _next_month(period):
    return date(period.year + (period.month == 12), period.month % 12 + 1, 1)


def _load_task_evidence(periods, cycles):
    """Batch all reads once, with no query chain per company or reporting period."""
    cycles_by_period = {cycle.obdobje: cycle for cycle in cycles}
    batches_by_cycle = defaultdict(list)
    for batch in MonthlyBatch.objects.filter(cycle_id__in=[cycle.pk for cycle in cycles]).order_by(
        "cycle_id", "company_id",
    ).values(
        "cycle_id", "company_id", "calculation_status", "calculation_schema_version",
        "calculation_snapshot",
    ):
        batches_by_cycle[batch["cycle_id"]].append(batch)
    memberships = {}
    for period in periods:
        cycle = cycles_by_period.get(period)
        memberships[period] = _monthly_membership(
            period, cycle, batches_by_cycle[cycle.pk] if cycle else (),
        )
    company_ids = {
        company_id for membership, _ in memberships.values() for company_id, _ in membership
    }
    latest = {}
    accepted = {}
    issued = defaultdict(set)
    if not company_ids:
        return memberships, latest, accepted, issued
    for attempt in Attempt.objects.filter(
        report__company_id__in=company_ids, report__period__in=periods,
    ).select_related("report"):
        key = (attempt.report.period, attempt.report.company_id)
        if key not in latest or (attempt.prepared_at, attempt.pk) > (
            latest[key].prepared_at, latest[key].pk,
        ):
            latest[key] = attempt
        if attempt.result == Attempt.Result.ACCEPTED:
            # Completion is frozen by Accounting 4. PK is the cross-report tie-breaker.
            order = (attempt.completed_at or attempt.prepared_at, attempt.pk)
            previous = accepted.get(key)
            if previous is None or order > (previous.completed_at or previous.prepared_at, previous.pk):
                accepted[key] = attempt
    for company_id, issued_on, number, kind in issued_invoice_queryset(
        company_ids=company_ids, period=min(periods), period_end=_next_month(max(periods)),
    ).values_list("company_id", "datum_izdaje", "stevilka", "document_type"):
        issued[(issued_on.replace(day=1), company_id)].add((number, kind))
    return memberships, latest, accepted, issued


def _company_states(membership, period, latest, accepted, issued):
    rows = []
    for company_id, label in membership:
        key = (period, company_id)
        sent = accepted.get(key)
        attempt = latest.get(key)
        stale = False
        if sent:
            represented = {
                (row["invoice_number"], row["document_type"])
                for row in sent.report.snapshot["issued_rows"]
            }
            stale = bool(issued[key] - represented)
            state = "STALE" if stale else "CURRENT"
            issue = "NEW_ISSUED_DOCUMENT" if stale else ""
        else:
            state = _ATTEMPT_STATES[attempt.result] if attempt else "NOT_SENT"
            issue = attempt.result if attempt else "NO_ACCEPTED_REPORT"
        rows.append(CompanyTaskState(
            company_id=company_id, label=label, state=state,
            accepted_report_id=sent.report_id if sent else None,
            latest_attempt_id=attempt.pk if attempt else None,
            latest_attempt_result=attempt.result if attempt else None,
            stale=stale, issue_code=issue,
        ))
    return tuple(rows)


def _validate_deadline_day(deadline_day):
    if type(deadline_day) is not int or not 1 <= deadline_day <= 31:
        raise ValueError("deadline_day must be an integer from 1 to 31.")


def _accountant_control_task_for_period(period, *, today, deadline_day, evidence):
    deadline = _next_month(period).replace(day=deadline_day)
    effective = next_slovenian_working_day(deadline)
    memberships, latest, accepted, issued = evidence
    membership, issue = memberships[period]
    rows = _company_states(membership, period, latest, accepted, issued)
    ready = not issue
    if ready and rows and all(row.state == "CURRENT" for row in rows):
        state = TaskState.ZAKLJUCENO
    elif any(row.stale for row in rows):
        state = TaskState.ZASTARELO
    elif today > effective:
        state = TaskState.OPOZORILO
    elif any(row.state == "CURRENT" for row in rows):
        state = TaskState.DELNO
    else:
        state = TaskState.ODPRTO
    return AccountantControlTask(period, deadline, effective, state, rows, ready, issue)


def accountant_control_task_state(*, as_of=None, deadline_day=5) -> AccountantControlTask:
    """Evaluate the previous-month task, including completed states.

    deadline_day must exist in the month after the reporting period.
    as_of controls the local calendar, not a historical database reconstruction.
    """
    today = _local_date(as_of)
    _validate_deadline_day(deadline_day)
    period = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    cycles = list(MonthlyCycle.objects.filter(obdobje=period).only(
        "id", "obdobje", "status", "trenutni_korak",
    ))
    return _accountant_control_task_for_period(
        period, today=today, deadline_day=deadline_day,
        evidence=_load_task_evidence((period,), cycles),
    )


def accountant_control_dashboard_tasks(*, as_of=None, deadline_day=5) -> tuple[AccountantControlTask, ...]:
    """Return active previous-month and persisted historical tasks, oldest first."""
    today = _local_date(as_of)
    _validate_deadline_day(deadline_day)
    current_month = today.replace(day=1)
    previous_month = (current_month - timedelta(days=1)).replace(day=1)
    cycles = list(MonthlyCycle.objects.filter(obdobje__lt=current_month).only(
        "id", "obdobje", "status", "trenutni_korak",
    ))
    periods = sorted({previous_month, *(cycle.obdobje for cycle in cycles)})
    evidence = _load_task_evidence(periods, cycles)
    tasks = (
        _accountant_control_task_for_period(
            period, today=today, deadline_day=deadline_day, evidence=evidence,
        )
        for period in periods
    )
    return tuple(task for task in tasks if task.active)
