"""Explicit POST actions for interrupted report mail and manual verification."""

from datetime import date

from django.db import transaction
from django.shortcuts import redirect
from django.views.decorators.http import require_POST

from archive.configuration import resolve_archive_roots
from delivery.models import AccountantReportDeliveryAttempt, MonthlySummaryAttempt
from delivery.report_recovery import (
    _scope,
    has_report_sent_evidence,
    recover_accountant_report_submissions,
    recover_monthly_summary_submissions,
    resolve_accountant_report_as_sent,
    resolve_accountant_report_not_sent,
    resolve_monthly_summary_as_sent,
    resolve_monthly_summary_not_sent,
)


@transaction.non_atomic_requests
@require_POST
def monthly_summary_recovery(request):
    error = False
    try:
        cycle_id = int(request.POST.get("cycle_id", ""))
        attempt_id = int(request.POST.get("attempt_id", ""))
        attempt = MonthlySummaryAttempt.objects.get(pk=attempt_id, cycle_id=cycle_id)
        _scope(attempt, cycle_id=cycle_id)
        action = request.POST.get("decision")
        if action == "recover":
            recover_monthly_summary_submissions(cycle_id=cycle_id, attempt_id=attempt_id)
        elif action in ("sent", "not_sent"):
            operation = resolve_monthly_summary_as_sent if action == "sent" else resolve_monthly_summary_not_sent
            operation(attempt, cycle_id=cycle_id, reason=request.POST.get("reason", ""))
        else:
            error = True
    except Exception:  # noqa: BLE001 - return only fixed, safe feedback
        error = True
    response = redirect("ui:mesecni_obracun")
    if error:
        response["Location"] += "?summary_error=recovery"
    return response


@transaction.non_atomic_requests
@require_POST
def accountant_report_recovery(request):
    code = "ACTION_FAILED"
    try:
        company_id = int(request.POST.get("company_id", ""))
        period = date(int(request.POST.get("year", "")), int(request.POST.get("month", "")), 1)
        attempt = AccountantReportDeliveryAttempt.objects.get(
            pk=int(request.POST.get("attempt_id", "")),
            report_id=int(request.POST.get("report_id", "")),
            report__company_id=company_id, report__period=period,
        )
        _scope(attempt, company_id=company_id, period=period)
        action = request.POST.get("decision")
        if action == "recover":
            recover_accountant_report_submissions(company_id=company_id, period=period, attempt_id=attempt.pk)
            current = type(attempt).objects.get(pk=attempt.pk)
            code = "CURRENT" if has_report_sent_evidence(current) else (
                current.result if current.result in ("UNKNOWN", "SUBMISSION_STARTED") else "ACTION_FAILED"
            )
        elif action == "sent":
            resolve_accountant_report_as_sent(attempt, company_id=company_id, period=period, reason=request.POST.get("reason", ""))
            code = "SENT_VERIFIED"
        elif action == "not_sent":
            resolve_accountant_report_not_sent(
                attempt, company_id=company_id, period=period,
                reason=request.POST.get("reason", ""), local_root=resolve_archive_roots().local_root,
            )
            code = "RETRY_PREPARED"
    except Exception:  # noqa: BLE001 - never expose filesystem, SMTP or credential details
        code = "ACTION_FAILED"
    response = redirect("ui:dashboard")
    response["Location"] += f"?accountant_result={code}"
    return response
