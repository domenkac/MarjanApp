import re

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from contracts.models import RecipientContact
from invoicing.models import Invoice

from .models import DeliveryAttempt

_CODE_RE = re.compile(r"^[A-Z0-9_]{1,64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_AUTHORIZATION_RE = re.compile(
    r"(?i)(\bauthorization\s*:\s*)bearer\s+(?:\"[^\"]*\"|'[^']*'|\S+)"
)
_SECRET_RE = re.compile(
    r"(?i)(\b(?:password|passwd|secret|token|credential)\b\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|\S+)"
)
_BEARER_RE = re.compile(r"(?i)(\bbearer\s+)(?:\"[^\"]*\"|'[^']*'|\S+)")


def _sanitize(code: str, message: str) -> tuple[str, str]:
    code = (code or "").strip()
    if not _CODE_RE.fullmatch(code):
        raise ValidationError("Result code must use stable uppercase letters, digits, or underscores.")
    message = _AUTHORIZATION_RE.sub(r"\1[REDACTED]", (message or "").strip())
    message = _SECRET_RE.sub(r"\1[REDACTED]", message)
    return code, _BEARER_RE.sub(r"\1[REDACTED]", message)


def _recipient_snapshots(invoice: Invoice) -> dict[str, list[str]]:
    contacts = RecipientContact.objects.filter(
        lease_id=invoice.lease_id,
        aktiven=True,
        kanal__in=[RecipientContact.Kanal.PDF, RecipientContact.Kanal.OBOJE],
    ).order_by("pk")
    result = {"TO": [], "CC": [], "BCC": []}
    seen = {role: set() for role in result}
    for contact in contacts:
        address = contact.email.strip()
        key = address.casefold()
        if key not in seen[contact.vloga]:
            result[contact.vloga].append(address)
            seen[contact.vloga].add(key)
    accountant = invoice.company.email_racunovodja.strip()
    if accountant.casefold() not in seen["BCC"]:
        result["BCC"].append(accountant)
    return result


def prepare_attempt(invoice: Invoice) -> DeliveryAttempt:
    with transaction.atomic():
        current = Invoice.objects.select_related("company").get(pk=invoice.pk)
        if current.status != Invoice.Status.IZBRAN_ZA_POSILJANJE:
            raise ValidationError("Only a selected invoice can be prepared for delivery.")
        if not current.stevilka:
            raise ValidationError("Invoice must be issued before delivery.")
        if not current.rendered_pdf_path or not _SHA256_RE.fullmatch(current.checksum or ""):
            raise ValidationError("Valid registered final PDF metadata is required.")
        if current.lease_id is None:
            raise ValidationError("Invoice must have a lease with delivery recipients.")
        if DeliveryAttempt.objects.filter(invoice=current).exists():
            raise ValidationError("Invoice already has a delivery attempt.")

        recipients = _recipient_snapshots(current)
        validate_email(current.company.email_posiljatelj)
        sequence = (
            DeliveryAttempt.objects.filter(invoice=current).aggregate(Max("sequence"))[
                "sequence__max"
            ]
            or 0
        ) + 1
        attempt = DeliveryAttempt(
            invoice=current,
            sequence=sequence,
            sender=current.company.email_posiljatelj,
            recipients_to=recipients["TO"],
            recipients_cc=recipients["CC"],
            recipients_bcc=recipients["BCC"],
            pdf_path=current.rendered_pdf_path,
            pdf_checksum=current.checksum,
        )
        attempt.full_clean()
        attempt.save()
        return attempt


def mark_submission_started(attempt: DeliveryAttempt) -> DeliveryAttempt:
    return _transition(
        attempt,
        expected=DeliveryAttempt.Result.PREPARED,
        result=DeliveryAttempt.Result.SUBMISSION_STARTED,
        submission_started_at=timezone.now(),
    )


def mark_accepted(attempt: DeliveryAttempt, *, code: str, message: str = "") -> DeliveryAttempt:
    code, message = _sanitize(code, message)
    return _transition_with_invoice(
        attempt,
        expected=DeliveryAttempt.Result.SUBMISSION_STARTED,
        result=DeliveryAttempt.Result.ACCEPTED,
        invoice_status=Invoice.Status.POSLAN,
        code=code,
        message=message,
    )


def mark_pre_submission_failure(
    attempt: DeliveryAttempt, *, code: str, message: str = ""
) -> DeliveryAttempt:
    code, message = _sanitize(code, message)
    return _transition_with_invoice(
        attempt,
        expected=DeliveryAttempt.Result.PREPARED,
        result=DeliveryAttempt.Result.FAILED_BEFORE_SUBMISSION,
        invoice_status=Invoice.Status.NAPAKA,
        code=code,
        message=message,
    )


def mark_result_unknown(
    attempt: DeliveryAttempt, *, code: str, message: str = ""
) -> DeliveryAttempt:
    code, message = _sanitize(code, message)
    return _transition_with_invoice(
        attempt,
        expected=DeliveryAttempt.Result.SUBMISSION_STARTED,
        result=DeliveryAttempt.Result.UNKNOWN,
        invoice_status=Invoice.Status.REZULTAT_NI_ZNAN,
        code=code,
        message=message,
    )


def is_retry_eligible(invoice: Invoice) -> bool:
    return Invoice.objects.filter(pk=invoice.pk, status=Invoice.Status.NAPAKA).exists()


def _transition(attempt, *, expected, result, **values) -> DeliveryAttempt:
    updated = DeliveryAttempt.objects.filter(pk=attempt.pk, result=expected).update(
        result=result, **values
    )
    if updated != 1:
        raise ValidationError(f"Invalid delivery transition; expected {expected}.")
    return DeliveryAttempt.objects.get(pk=attempt.pk)


def _transition_with_invoice(
    attempt, *, expected, result, invoice_status, code, message
) -> DeliveryAttempt:
    with transaction.atomic():
        current = DeliveryAttempt.objects.get(pk=attempt.pk)
        if current.result != expected:
            raise ValidationError(f"Invalid delivery transition; expected {expected}.")
        updated = Invoice.objects.filter(
            pk=current.invoice_id,
            status=Invoice.Status.IZBRAN_ZA_POSILJANJE,
        ).update(status=invoice_status)
        if updated != 1:
            raise ValidationError("Invoice status no longer permits this delivery transition.")
        DeliveryAttempt.objects.filter(pk=current.pk, result=expected).update(
            result=result,
            result_code=code,
            message=message,
            completed_at=timezone.now(),
        )
    return DeliveryAttempt.objects.get(pk=attempt.pk)
