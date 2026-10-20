"""Resumable accountant control action; filesystem and SMTP work outside transactions."""

from dataclasses import dataclass, replace
from datetime import date

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import DatabaseError, connection, transaction

from archive.configuration import ArchiveConfigurationError, resolve_archive_roots
from companies.models import Company
from delivery.configuration import (
    SmtpConfigurationError,
    resolve_keyring_credential_ref,
    resolve_smtp_config_for_username,
)
from delivery.models import AccountantReportDeliveryAttempt as Attempt
from delivery.report_recovery import has_report_sent_evidence, unresolved
from delivery.services import (
    prepare_accountant_report_delivery,
    prepare_accountant_report_delivery_retry,
    send_accountant_report,
)
from delivery.verification import SmtpVerificationRequired, require_verified

from .archive import (
    archive_accountant_report_pdf,
    verify_accountant_report_pdf_artifact,
)
from .models import AccountantReport, AccountantReportPdfArtifact
from .services import create_accountant_report, issued_invoice_queryset
from .task_state import accountant_control_dashboard_tasks

FEEDBACK = {
    "SMTP_VERIFICATION_REQUIRED": "SMTP povezava ni potrjena. V Nastavitve > Podjetje izvedite Preveri SMTP povezavo.",
    "SENT_VERIFIED": "Pošiljanje je uporabnik preveril kot poslano. Izvirni rezultat UNKNOWN ostaja v revizijski sledi.",
    "RETRY_PREPARED": "Ponovitev je pripravljena. Za pošiljanje uporabite ločeno dejanje Pošlji.",
    "ACCEPTED": "Kontrolni dokument je bil poslan.",
    "CURRENT": "Aktualni kontrolni dokument je že poslan.",
    "UNKNOWN": "Rezultat pošiljanja ni znan – potrebno je preverjanje. Samodejno ponovno pošiljanje ni na voljo.",
    "SUBMISSION_STARTED": "Pošiljanje je v teku – potrebno je preverjanje. Samodejno ponovno pošiljanje ni na voljo.",
    "CONFIRMED_FAILURE": "Pošiljanje ni uspelo. Pošiljanje lahko ponovite.",
    "INVALID_ACTION": "Podjetje ali obdobje ni del pripravljenega aktivnega opravila.",
    "ARCHIVE_CONFIGURATION": "Lokacija lokalnega arhiva ni konfigurirana.",
    "ARCHIVE_MISSING": "Lokalni PDF manjka. Potrebno je preverjanje arhiva.",
    "ARCHIVE_CHECKSUM_MISMATCH": "Kontrolni odtis lokalnega PDF se ne ujema. Potrebno je preverjanje arhiva.",
    "ARCHIVE_METADATA_CONFLICT": "Podatki lokalnega PDF so v sporu. Potrebno je preverjanje arhiva.",
    "ARCHIVE_FAILED": "Lokalnega PDF ni mogoče preveriti ali shraniti.",
    "STALE": "Podatki so se spremenili. Poročilo ni bilo poslano; nadaljujte z novim poročilom.",
    "SENDER_CHANGED": "Pošiljatelj se razlikuje od zamrznjenega pošiljatelja. Potrebno je preverjanje nastavitev.",
    "SMTP_CONFIGURATION": "SMTP nastavitve niso veljavne. Pripravljeno pošiljanje lahko nadaljujete po popravku nastavitev.",
    "CREDENTIAL_FAILED": "Poverilnice niso dostopne. Pripravljeno pošiljanje lahko nadaljujete po preverjanju nastavitev.",
    "NO_ISSUED_SNAPSHOT": "Poročila ni mogoče pripraviti: manjka posnetek izdanega računa za zgodovinske podatke podjetja.",
    "ISSUER_CONFLICT": "Poročila ni mogoče pripraviti: zgodovinski podatki izdajatelja se ne ujemajo.",
    "REPORT_REJECTED": "Poročila ni mogoče pripraviti zaradi neustreznih mesečnih podatkov.",
    "PREPARATION_FAILED": "Pošiljanja ni mogoče pripraviti. Preverite prejemnika, pošiljatelja in shranjene podatke.",
    "ACTION_FAILED": "Dejanja ni mogoče dokončati. Preverite stanje opravila pred nadaljevanjem.",
}


@dataclass(frozen=True)
class ControlActionResult:
    code: str
    report_id: int | None = None
    attempt_id: int | None = None

    @property
    def message(self):
        return FEEDBACK[self.code]


def _attempts(company_id, period):
    return Attempt.objects.filter(report__company_id=company_id, report__period=period)


def _blocked(company_id, period):
    results = {
        attempt.result for attempt in _attempts(company_id, period)
        .filter(
            result__in=(Attempt.Result.UNKNOWN, Attempt.Result.SUBMISSION_STARTED),
        )
        if unresolved(attempt)
    }
    if Attempt.Result.UNKNOWN in results:
        return "UNKNOWN"
    if Attempt.Result.SUBMISSION_STARTED in results:
        return "SUBMISSION_STARTED"
    return None


def report_is_current(report, *, issued=None):
    represented = {
        (row["invoice_number"], row["document_type"])
        for row in report.snapshot["issued_rows"]
    }
    if issued is None:
        issued = set(
            issued_invoice_queryset(
                company_ids=[report.company_id],
                period=report.period,
            ).values_list("stevilka", "document_type")
        )
    return represented == issued


def _active_company(company_id, period, as_of):
    return any(
        task.period == period
        and task.ready
        and any(row.company_id == company_id for row in task.companies)
        for task in accountant_control_dashboard_tasks(as_of=as_of)
    )


def accountant_control_action_tasks(tasks):
    """Read-only UI projection; unresolved historical attempts override row labels."""
    blocked = {}
    for attempt in Attempt.objects.filter(
        report__period__in=[task.period for task in tasks],
        result__in=(Attempt.Result.UNKNOWN, Attempt.Result.SUBMISSION_STARTED),
    ).select_related("report"):
        if not unresolved(attempt):
            continue
        key = (attempt.report.company_id, attempt.report.period)
        if blocked.get(key) != "UNKNOWN":
            blocked[key] = attempt.result
    attempt_ids = {
        row.latest_attempt_id
        for task in tasks
        for row in task.companies
        if row.state in ("PREPARED", "FAILED") and row.latest_attempt_id is not None
    }
    attempts = {
        attempt.pk: attempt
        for attempt in Attempt.objects.filter(pk__in=attempt_ids).select_related(
            "report"
        )
    }
    issued_by_company_period = {}
    if attempts:
        periods = [attempt.report.period for attempt in attempts.values()]
        last = max(periods)
        period_end = date(last.year + (last.month == 12), last.month % 12 + 1, 1)
        for company_id, issued_on, number, kind in issued_invoice_queryset(
            company_ids={attempt.report.company_id for attempt in attempts.values()},
            period=min(periods),
            period_end=period_end,
        ).values_list("company_id", "datum_izdaje", "stevilka", "document_type"):
            issued_by_company_period.setdefault(
                (company_id, issued_on.replace(day=1)), set()
            ).add((number, kind))
    labels = {
        "NOT_SENT": "Pripravi in pošlji",
        "PREPARED": "Pošlji",
        "FAILED": "Ponovi pošiljanje",
        "STALE": "Pripravi novo in pošlji",
    }
    decorated = []
    for task in tasks:
        rows = []
        for row in task.companies:
            block = blocked.get((row.company_id, task.period))
            label = labels.get(row.state, "") if task.ready and not block else ""
            attempt = attempts.get(row.latest_attempt_id)
            outdated = attempt is not None and not report_is_current(
                attempt.report,
                issued=issued_by_company_period.get(
                    (row.company_id, task.period), set()
                ),
            )
            if (
                label
                and (row.stale or outdated)
                and row.latest_attempt_result
                in (
                    Attempt.Result.PREPARED,
                    Attempt.Result.CONFIRMED_FAILURE,
                )
            ):
                label = "Nadaljuj"
            rows.append(
                replace(
                    row,
                    action_label=label,
                    action_blocked_message=FEEDBACK[block] if block else "",
                )
            )
        decorated.append(replace(task, companies=tuple(rows)))
    return tuple(decorated)


def _first_attempt_preflight(company_id):
    """Check only DB values and basic configuration; never load credentials or use IO."""
    try:
        company = Company.objects.get(pk=company_id)
        validate_email(company.email_posiljatelj)
        validate_email(company.email_racunovodja)
    except (Company.DoesNotExist, ValidationError):
        return "PREPARATION_FAILED"
    try:
        resolve_smtp_config_for_username(company.email_posiljatelj)
        resolve_keyring_credential_ref(company)
    except SmtpConfigurationError:
        return "SMTP_CONFIGURATION"
    return None


def _choose_report(company_id, period, as_of):
    # Only selection/creation is serialized. SQLite's configured IMMEDIATE mode
    # prevents two initial actions from creating independent sending gates.
    with transaction.atomic():
        blocked = _blocked(company_id, period)
        if blocked:
            return None, blocked
        reports = list(
            AccountantReport.objects.filter(
                company_id=company_id,
                period=period,
            ).order_by("-generated_at", "-pk")
        )
        current = [report for report in reports if report_is_current(report)]
        if any(
            any(has_report_sent_evidence(attempt) for attempt in report.delivery_attempts.all())
            for report in current
        ):
            return None, "CURRENT"
        if not _active_company(company_id, period, as_of):
            return None, "INVALID_ACTION"
        report = current[0] if current else None
        if report is None or not report.delivery_attempts.exists():
            code = _first_attempt_preflight(company_id)
            if code:
                return report, code
        if current:
            return report, None
        return create_accountant_report(
            company=Company.objects.get(pk=company_id),
            period=period,
        ), None


def _verify_before_send(report_id, attempt_id, local_root):
    attempt = Attempt.objects.get(pk=attempt_id)
    report = AccountantReport.objects.get(pk=report_id)
    blocked = _blocked(report.company_id, report.period)
    if blocked:
        return blocked
    if attempt.report_id != report.pk or attempt.result != Attempt.Result.PREPARED:
        return (
            "CURRENT" if attempt.result == Attempt.Result.ACCEPTED else "ACTION_FAILED"
        )
    latest = report.delivery_attempts.order_by("-sequence").first()
    if latest.pk != attempt.pk:
        return "ACTION_FAILED"
    if not report_is_current(report):
        return "STALE"
    artifact = AccountantReportPdfArtifact.objects.filter(report=report).first()
    if artifact is None:
        return "ARCHIVE_MISSING"
    verified = verify_accountant_report_pdf_artifact(artifact, local_root)
    return None if verified.success else f"ARCHIVE_{verified.state}"


def deliver_accountant_control_report(
    *,
    company_id,
    period,
    as_of=None,
    password_loader=None,
    smtp_factory=None,
):
    """Resume only authoritative persisted evidence. No attempt IDs from the caller."""
    if connection.in_atomic_block:
        raise ValidationError("Accountant workflow must run outside a DB transaction.")
    if (
        type(company_id) is not int
        or company_id <= 0
        or type(period) is not date
        or period.day != 1
    ):
        return ControlActionResult("INVALID_ACTION")
    blocked = _blocked(company_id, period)
    if blocked:
        return ControlActionResult(blocked)
    try:
        roots = resolve_archive_roots()
    except ArchiveConfigurationError:
        return ControlActionResult("ARCHIVE_CONFIGURATION")
    try:
        report, code = _choose_report(company_id, period, as_of)
    except ValidationError as exc:
        code = "REPORT_REJECTED"
        if exc.messages == [
            "Accountant report has no issued invoice snapshot for its historical header."
        ]:
            code = "NO_ISSUED_SNAPSHOT"
        elif exc.messages == [
            "Issued invoice issuer identity snapshots conflict within the reporting month."
        ]:
            code = "ISSUER_CONFLICT"
        return ControlActionResult(code)
    except DatabaseError:
        return ControlActionResult("ACTION_FAILED")
    if code:
        return ControlActionResult(code)
    try:
        archived = archive_accountant_report_pdf(report, roots.local_root)
        if not archived.success:
            return ControlActionResult(f"ARCHIVE_{archived.state}", report.pk)
        attempt = report.delivery_attempts.order_by("-sequence").first()
        if attempt is None:
            attempt = prepare_accountant_report_delivery(
                report, local_root=roots.local_root
            )
        elif attempt.result == Attempt.Result.CONFIRMED_FAILURE:
            attempt = prepare_accountant_report_delivery_retry(
                attempt, local_root=roots.local_root
            )
        elif attempt.result != Attempt.Result.PREPARED:
            return ControlActionResult(
                "CURRENT"
                if has_report_sent_evidence(attempt)
                else attempt.result,
                report.pk,
                attempt.pk,
            )
    except (ValidationError, DatabaseError):
        return ControlActionResult("PREPARATION_FAILED", report.pk)
    try:
        company = Company.objects.get(pk=company_id)
        if company.email_posiljatelj != attempt.sender:
            return ControlActionResult("SENDER_CHANGED", report.pk, attempt.pk)
        config = resolve_smtp_config_for_username(attempt.sender)
        credential = resolve_keyring_credential_ref(company)
    except SmtpConfigurationError:
        return ControlActionResult("SMTP_CONFIGURATION", report.pk, attempt.pk)
    code = _verify_before_send(report.pk, attempt.pk, roots.local_root)
    if code:
        return ControlActionResult(code, report.pk, attempt.pk)

    try:
        require_verified(company_id, config, credential, attempt.sender)
    except SmtpVerificationRequired:
        return ControlActionResult("SMTP_VERIFICATION_REQUIRED", report.pk, attempt.pk)

    try:
        from delivery.credentials import load_password

        current_company = Company.objects.get(pk=company_id)
        if current_company.email_posiljatelj != attempt.sender:
            return ControlActionResult("SENDER_CHANGED", report.pk, attempt.pk)
        current_ref = resolve_keyring_credential_ref(current_company)
        password = (password_loader or load_password)(current_ref)
        if not isinstance(password, str) or not password:
            return ControlActionResult("CREDENTIAL_FAILED", report.pk, attempt.pk)
    except Exception:  # noqa: BLE001 - credential errors must never expose secrets
        return ControlActionResult("CREDENTIAL_FAILED", report.pk, attempt.pk)

    # Keychain may wait for user interaction. Recheck after it returns, before
    # handing the ephemeral password to Accounting 4 and entering the network.
    code = _verify_before_send(report.pk, attempt.pk, roots.local_root)
    if code:
        return ControlActionResult(code, report.pk, attempt.pk)
    if Company.objects.get(pk=company_id).email_posiljatelj != attempt.sender:
        return ControlActionResult("SENDER_CHANGED", report.pk, attempt.pk)

    try:
        sent = send_accountant_report(
            attempt,
            config,
            credential,
            password_loader=lambda _: password,
            smtp_factory=smtp_factory,
        )
        return ControlActionResult(sent.result, report.pk, attempt.pk)
    except SmtpVerificationRequired:
        return ControlActionResult("SMTP_VERIFICATION_REQUIRED", report.pk, attempt.pk)
    except (ValidationError, DatabaseError):
        # A competing transition is authoritative; never repair or overwrite it.
        code = _blocked(company_id, period) or "ACTION_FAILED"
        return ControlActionResult(code, report.pk, attempt.pk)
