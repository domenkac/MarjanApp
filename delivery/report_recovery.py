"""Audited recovery for report mail. Raw SMTP evidence is never rewritten."""

from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.utils import timezone

from core.models import AuditLogEntry
from invoicing.models import MonthlyCycle

from .execution import report_scope_execution_lock
from .models import AccountantReportDeliveryAttempt as AccountantAttempt
from .models import MonthlySummaryAttempt as MonthlyAttempt

SENT_VERIFIED = "SENT_VERIFIED"
NOT_SENT_CONFIRMED = "NOT_SENT_CONFIRMED"


def resolution(attempt):
    return AuditLogEntry.objects.filter(
        content_type__app_label=attempt._meta.app_label,
        content_type__model=attempt._meta.model_name,
        object_id=attempt.pk, polje="unknown_resolution",
        stara_vrednost="UNRESOLVED",
    ).values_list("nova_vrednost", flat=True).first()


def has_report_sent_evidence(attempt):
    return attempt.result == "ACCEPTED" or (
        attempt.result == "UNKNOWN" and resolution(attempt) == SENT_VERIFIED
    )


def unresolved(attempt):
    return attempt.result == "SUBMISSION_STARTED" or (
        attempt.result == "UNKNOWN" and resolution(attempt) is None
    )


@contextmanager
def report_execution_lock(attempt):
    if isinstance(attempt, MonthlyAttempt):
        kind, identity = "monthly", attempt.cycle_id
    elif isinstance(attempt, AccountantAttempt):
        kind, identity = "accountant", f"{attempt.report.company_id}-{attempt.report.period.isoformat()}"
    else:
        raise ValidationError("Unsupported report attempt.")
    with report_scope_execution_lock(kind, identity) as acquired:
        yield acquired


def require_outside_transaction():
    if connection.in_atomic_block:
        raise ValidationError("Report delivery must run outside a DB transaction.")


def require_frozen_attempt(attempt):
    current = type(attempt).objects.get(pk=attempt.pk)
    excluded = {"result", "result_code", "message", "completed_at", "submission_started_at"}
    if any(
        getattr(attempt, field.attname) != getattr(current, field.attname)
        for field in current._meta.concrete_fields if field.name not in excluded
    ):
        raise ValidationError("Frozen report delivery metadata changed.")
    return current


def require_monthly_current(attempt, *, state=None):
    from invoicing.services import stanje_zakljucne_kontrole_mesecnega_cikla

    from .services import _monthly_summary_body, _monthly_summary_snapshot

    snapshot = attempt.summary_snapshot
    try:
        prepared_at = datetime.fromisoformat(snapshot["prepared_at"])
        expected = _monthly_summary_snapshot(
            attempt.cycle,
            state or stanje_zakljucne_kontrole_mesecnega_cikla(attempt.cycle),
            prepared_at,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError("Monthly summary snapshot is incomplete or stale.") from exc
    if snapshot != expected or attempt.body != _monthly_summary_body(snapshot):
        raise ValidationError("Monthly summary is stale; another summary requires a product decision.")


def _scope(attempt, *, cycle_id=None, company_id=None, period=None):
    if isinstance(attempt, MonthlyAttempt):
        if type(cycle_id) is not int or attempt.cycle_id != cycle_id:
            raise ValidationError("Monthly cycle does not own this attempt.")
        cycle = MonthlyCycle.objects.get(pk=cycle_id)
        if cycle.status != MonthlyCycle.Status.V_TEKU or cycle.trenutni_korak != MonthlyCycle.Korak.ZAKLJUCEK:
            raise ValidationError("Monthly cycle must remain active in Step 6.")
        return MonthlyAttempt.objects.filter(cycle_id=cycle_id)
    if (
        type(company_id) is not int or type(period) is not date or period.day != 1
        or (attempt.report.company_id, attempt.report.period) != (company_id, period)
    ):
        raise ValidationError("Company and period do not own this attempt.")
    from accounting_export.workflow import _active_company

    if not _active_company(company_id, period, None):
        raise ValidationError("Company and period are not an authorized monthly task.")
    return AccountantAttempt.objects.filter(report__company_id=company_id, report__period=period)


def _latest_unknown(attempt, scope):
    current = type(attempt).objects.get(pk=attempt.pk)
    attempts = _scope(current, **scope)
    relevant = attempts if isinstance(current, MonthlyAttempt) else attempts.filter(report_id=current.report_id)
    if relevant.order_by("-sequence").first().pk != current.pk:
        raise ValidationError("Only the latest relevant attempt can be resolved.")
    if current.result != "UNKNOWN" or resolution(current) is not None:
        raise ValidationError("Only an unresolved UNKNOWN attempt can be resolved.")
    # Reject dirty caller snapshots and metadata altered during external checks.
    require_frozen_attempt(attempt)
    for item in attempts:
        same_report = isinstance(current, MonthlyAttempt) or item.report_id == current.report_id
        if same_report and has_report_sent_evidence(item):
            raise ValidationError("Conflicting delivery evidence exists.")
        if item.pk != current.pk and unresolved(item) and (item.prepared_at, item.pk) > (current.prepared_at, current.pk):
            raise ValidationError("Resolve the latest unresolved report attempt first.")
    return current


def _audit(attempt, field, old, new, reason):
    AuditLogEntry.objects.create(
        content_type=ContentType.objects.get_for_model(type(attempt)), object_id=attempt.pk,
        polje=field, stara_vrednost=old, nova_vrednost=new, razlog=reason,
    )


def _resolve(attempt, *, reason, decision, scope, local_root=None):
    from .services import (
        _ACCOUNTANT_FROZEN_FIELDS,
        _accountant_attachment,
        _accountant_report,
        _required_text,
        _sanitize,
        _verify_accountant_pdf,
    )

    require_outside_transaction()
    reason = _required_text(reason, "Reason")
    _, reason = _sanitize("MANUAL_VERIFICATION", reason)
    if attempt.pk is None or attempt._state.adding:
        raise ValidationError("Report attempt must be persisted.")
    with report_execution_lock(attempt) as acquired:
        if not acquired:
            raise ValidationError("Report delivery is already active.")
        current = _latest_unknown(attempt, scope)
        report = None
        if decision == NOT_SENT_CONFIRMED:
            if isinstance(current, MonthlyAttempt):
                require_monthly_current(current)
                fields = ("sender", "recipient", "subject", "body", "summary_snapshot")
            else:
                from accounting_export.workflow import report_is_current

                report = _accountant_report(current.report_id)
                if not report_is_current(report):
                    raise ValidationError("Accountant source evidence is stale.")
                _verify_accountant_pdf(report, local_root)
                _accountant_attachment(report, current)
                fields = _ACCOUNTANT_FROZEN_FIELDS
            frozen = {field: deepcopy(getattr(current, field)) for field in fields}
        with transaction.atomic():
            current = _latest_unknown(current, scope)
            if report is not None:
                from .services import _accountant_identity_unchanged

                _accountant_identity_unchanged(report)
                if not report_is_current(report):
                    raise ValidationError("Accountant source evidence is stale.")
            _audit(current, "unknown_resolution", "UNRESOLVED", decision, reason)
            if decision == SENT_VERIFIED:
                return current
            parent = {"cycle_id": current.cycle_id} if isinstance(current, MonthlyAttempt) else {"report_id": current.report_id}
            return type(current).objects.create(sequence=current.sequence + 1, **parent, **frozen)


def resolve_monthly_summary_as_sent(attempt, *, cycle_id, reason):
    return _resolve(attempt, reason=reason, decision=SENT_VERIFIED, scope={"cycle_id": cycle_id})


def resolve_monthly_summary_not_sent(attempt, *, cycle_id, reason, local_root=None):
    return _resolve(attempt, reason=reason, decision=NOT_SENT_CONFIRMED, scope={"cycle_id": cycle_id})


def resolve_accountant_report_as_sent(attempt, *, company_id, period, reason):
    return _resolve(attempt, reason=reason, decision=SENT_VERIFIED, scope={"company_id": company_id, "period": period})


def resolve_accountant_report_not_sent(attempt, *, company_id, period, reason, local_root):
    return _resolve(attempt, reason=reason, decision=NOT_SENT_CONFIRMED, scope={"company_id": company_id, "period": period}, local_root=local_root)


def _recover(attempts):
    require_outside_transaction()
    recovered = 0
    for attempt in attempts.filter(result="SUBMISSION_STARTED"):
        with report_execution_lock(attempt) as acquired:
            if not acquired:
                continue
            with transaction.atomic():
                updated = type(attempt).objects.filter(pk=attempt.pk, result="SUBMISSION_STARTED").update(
                    result="UNKNOWN", completed_at=timezone.now(), result_code="SMTP_EXECUTION_INTERRUPTED",
                    message="Pošiljanje je prekinjeno brez shranjene potrditve. Preverite mapo Poslano; samodejna ponovitev je blokirana.",
                )
                if updated:
                    _audit(attempt, "result", "SUBMISSION_STARTED", "UNKNOWN",
                           "Exclusive report execution lock acquired; no active sender. SMTP acceptance is unknown. No resend performed.")
                    recovered += 1
    return recovered


def recover_monthly_summary_submissions(*, cycle_id, attempt_id=None):
    attempts = MonthlyAttempt.objects.filter(cycle_id=cycle_id)
    if attempt_id is not None:
        attempts = attempts.filter(pk=attempt_id)
    return _recover(attempts)


def recover_accountant_report_submissions(*, company_id, period, attempt_id=None):
    attempts = AccountantAttempt.objects.filter(report__company_id=company_id, report__period=period)
    if attempt_id is not None:
        attempts = attempts.filter(pk=attempt_id)
    return _recover(attempts)
