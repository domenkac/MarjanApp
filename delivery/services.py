import re
from dataclasses import dataclass
from email.message import EmailMessage

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from contracts.models import RecipientContact
from core.models import AuditLogEntry
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

UNKNOWN_RESOLUTION_FIELD = "unknown_resolution"
UNKNOWN_RESOLUTION_UNRESOLVED = "UNRESOLVED"
UNKNOWN_RESOLUTION_SENT_VERIFIED = "SENT_VERIFIED"
UNKNOWN_RESOLUTION_NOT_SENT_CONFIRMED = "NOT_SENT_CONFIRMED"


@dataclass(frozen=True, slots=True)
class PreparedEmail:
    message: EmailMessage
    envelope_sender: str
    envelope_recipients: tuple[str, ...]
    invoice_id: int
    attempt_id: int


def _required_text(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be a non-empty string.")
    return value.strip()


def _validated_addresses(addresses, name: str) -> tuple[str, ...]:
    if not isinstance(addresses, list):
        raise ValidationError(f"{name} must be a list of email addresses.")
    result = []
    for address in addresses:
        if not isinstance(address, str) or not address.strip():
            raise ValidationError(f"{name} contains an invalid email address.")
        address = address.strip()
        validate_email(address)
        result.append(address)
    return tuple(result)


def build_pdf_email(attempt: DeliveryAttempt, *, subject, body) -> PreparedEmail:
    subject = _required_text(subject, "Subject")
    body = _required_text(body, "Body")
    if "\r" in subject or "\n" in subject:
        raise ValidationError("Subject must not contain line breaks.")
    if attempt.pk is None:
        raise ValidationError("Delivery attempt must be persisted before composition.")
    current = DeliveryAttempt.objects.select_related("invoice").get(pk=attempt.pk)
    invoice = current.invoice
    if current.result != DeliveryAttempt.Result.PREPARED:
        raise ValidationError("Only a prepared delivery attempt can be composed.")
    if invoice.rendered_eslog_path or invoice.rendered_eslog_checksum:
        raise ValidationError("Unexpected eSLOG metadata blocks PDF-only delivery.")

    sender = current.sender.strip()
    validate_email(sender)
    recipients_to = _validated_addresses(current.recipients_to, "To recipients")
    recipients_cc = _validated_addresses(current.recipients_cc, "Cc recipients")
    recipients_bcc = _validated_addresses(current.recipients_bcc, "Bcc recipients")
    if not recipients_to:
        raise ValidationError("At least one To recipient is required.")

    from rendering.services import stanje_koncnega_pdf

    pdf_state = stanje_koncnega_pdf(invoice)
    if pdf_state["state"] != "READY":
        raise ValidationError("Registered final PDF failed integrity verification.")
    artifact = pdf_state["result"]
    expected = (invoice.rendered_pdf_path, invoice.checksum)
    if (artifact.pot, artifact.checksum) != expected or expected != (
        current.pdf_path,
        current.pdf_checksum,
    ):
        raise ValidationError("Final PDF metadata differs from the delivery snapshot.")

    envelope_recipients = []
    seen = set()
    for address in (*recipients_to, *recipients_cc, *recipients_bcc):
        key = address.casefold()
        if key not in seen:
            envelope_recipients.append(address)
            seen.add(key)

    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(recipients_to)
    if recipients_cc:
        message["Cc"] = ", ".join(recipients_cc)
    message["Subject"] = subject
    message.set_content(body)
    message.add_attachment(
        artifact.pdf_bytes,
        maintype="application",
        subtype="pdf",
        filename=artifact.pot.rsplit("/", 1)[-1],
    )
    return PreparedEmail(
        message=message,
        envelope_sender=sender,
        envelope_recipients=tuple(envelope_recipients),
        invoice_id=invoice.pk,
        attempt_id=current.pk,
    )


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


def prepare_retry_attempt(failed_attempt: DeliveryAttempt) -> DeliveryAttempt:
    if failed_attempt.pk is None:
        raise ValidationError("Delivery attempt must be persisted before retry.")

    with transaction.atomic():
        source = (
            DeliveryAttempt.objects.select_related("invoice")
            .filter(pk=failed_attempt.pk)
            .first()
        )
        if source is None:
            raise ValidationError("Delivery attempt must be persisted before retry.")
        invoice = source.invoice
        latest = (
            DeliveryAttempt.objects.filter(invoice=invoice)
            .order_by("-sequence")
            .first()
        )
        if source.result != DeliveryAttempt.Result.CONFIRMED_FAILURE:
            raise ValidationError("Only a confirmed failure can be retried.")
        if latest is None or latest.pk != source.pk:
            raise ValidationError("Only the latest delivery attempt can be retried.")
        if invoice.status != Invoice.Status.NAPAKA:
            raise ValidationError("Invoice status no longer permits retry.")
        if not invoice.stevilka:
            raise ValidationError("Invoice must be issued before retry.")
        if not source.pdf_path or not _SHA256_RE.fullmatch(source.pdf_checksum or ""):
            raise ValidationError("Valid PDF snapshot metadata is required for retry.")
        if (invoice.rendered_pdf_path, invoice.checksum) != (
            source.pdf_path,
            source.pdf_checksum,
        ):
            raise ValidationError("Invoice PDF metadata differs from the failed attempt.")

        retry = DeliveryAttempt(
            invoice=invoice,
            sequence=source.sequence + 1,
            sender=source.sender,
            recipients_to=source.recipients_to,
            recipients_cc=source.recipients_cc,
            recipients_bcc=source.recipients_bcc,
            pdf_path=source.pdf_path,
            pdf_checksum=source.pdf_checksum,
        )
        retry.full_clean()
        retry.save()
        updated = Invoice.objects.filter(
            pk=invoice.pk, status=Invoice.Status.NAPAKA
        ).update(status=Invoice.Status.IZBRAN_ZA_POSILJANJE)
        if updated != 1:
            raise ValidationError("Invoice status no longer permits retry.")
        return retry


def resolve_unknown_as_sent(
    attempt: DeliveryAttempt, *, reason: str
) -> DeliveryAttempt:
    reason = _required_text(reason, "Reason")
    with transaction.atomic():
        source, invoice, content_type = _unresolved_unknown(attempt)
        audit = _unknown_resolution_audit(
            source,
            content_type,
            reason,
            UNKNOWN_RESOLUTION_SENT_VERIFIED,
        )
        audit.full_clean()
        audit.save()
        updated = Invoice.objects.filter(
            pk=invoice.pk, status=Invoice.Status.REZULTAT_NI_ZNAN
        ).update(status=Invoice.Status.POSLAN)
        if updated != 1:
            raise ValidationError("Invoice status no longer permits UNKNOWN resolution.")
        return source


def resolve_unknown_user_confirmed_not_sent_and_prepare_retry(
    attempt: DeliveryAttempt, *, reason: str
) -> DeliveryAttempt:
    reason = _required_text(reason, "Reason")
    with transaction.atomic():
        source, invoice, content_type = _unresolved_unknown(attempt)
        if not invoice.stevilka:
            raise ValidationError("Invoice must be issued before retry.")
        if not source.pdf_path or not _SHA256_RE.fullmatch(source.pdf_checksum or ""):
            raise ValidationError("Valid PDF snapshot metadata is required for retry.")
        if (invoice.rendered_pdf_path, invoice.checksum) != (
            source.pdf_path,
            source.pdf_checksum,
        ):
            raise ValidationError("Invoice PDF metadata differs from the UNKNOWN attempt.")

        audit = _unknown_resolution_audit(
            source,
            content_type,
            reason,
            UNKNOWN_RESOLUTION_NOT_SENT_CONFIRMED,
        )
        audit.full_clean()
        audit.save()
        retry = DeliveryAttempt(
            invoice=invoice,
            sequence=source.sequence + 1,
            sender=source.sender,
            recipients_to=source.recipients_to,
            recipients_cc=source.recipients_cc,
            recipients_bcc=source.recipients_bcc,
            pdf_path=source.pdf_path,
            pdf_checksum=source.pdf_checksum,
        )
        retry.full_clean()
        retry.save()
        updated = Invoice.objects.filter(
            pk=invoice.pk, status=Invoice.Status.REZULTAT_NI_ZNAN
        ).update(status=Invoice.Status.IZBRAN_ZA_POSILJANJE)
        if updated != 1:
            raise ValidationError("Invoice status no longer permits UNKNOWN resolution.")
        return retry


def _unresolved_unknown(attempt: DeliveryAttempt):
    if attempt.pk is None:
        raise ValidationError("Delivery attempt must be persisted before resolution.")
    source = (
        DeliveryAttempt.objects.select_related("invoice").filter(pk=attempt.pk).first()
    )
    if source is None:
        raise ValidationError("Delivery attempt must be persisted before resolution.")
    invoice = source.invoice
    if source.result != DeliveryAttempt.Result.UNKNOWN:
        raise ValidationError("Only an UNKNOWN delivery attempt can be resolved.")
    latest = (
        DeliveryAttempt.objects.filter(invoice=invoice).order_by("-sequence").first()
    )
    if latest is None or latest.pk != source.pk:
        raise ValidationError("Only the latest delivery attempt can be resolved.")
    if invoice.status != Invoice.Status.REZULTAT_NI_ZNAN:
        raise ValidationError("Invoice status no longer permits UNKNOWN resolution.")
    content_type = ContentType.objects.get_for_model(DeliveryAttempt)
    if AuditLogEntry.objects.filter(
        content_type=content_type,
        object_id=source.pk,
        polje=UNKNOWN_RESOLUTION_FIELD,
    ).exists():
        raise ValidationError("UNKNOWN delivery attempt has already been resolved.")
    return source, invoice, content_type


def _unknown_resolution_audit(
    source: DeliveryAttempt, content_type, reason: str, resolution: str
) -> AuditLogEntry:
    return AuditLogEntry(
        content_type=content_type,
        object_id=source.pk,
        dejanje=AuditLogEntry.Dejanje.SPREMEMBA,
        polje=UNKNOWN_RESOLUTION_FIELD,
        stara_vrednost=UNKNOWN_RESOLUTION_UNRESOLVED,
        nova_vrednost=resolution,
        razlog=reason,
    )


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


def mark_confirmed_failure(
    attempt: DeliveryAttempt, *, code: str, message: str = ""
) -> DeliveryAttempt:
    if attempt.result not in (
        DeliveryAttempt.Result.PREPARED,
        DeliveryAttempt.Result.SUBMISSION_STARTED,
    ):
        raise ValidationError("Only a prepared or started delivery can fail.")
    code, message = _sanitize(code, message)
    return _transition_with_invoice(
        attempt,
        expected=attempt.result,
        result=DeliveryAttempt.Result.CONFIRMED_FAILURE,
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


def send_pdf_email(
    attempt: DeliveryAttempt,
    *,
    subject,
    body,
    smtp_config,
    credential_ref,
    password_loader=None,
    smtp_factory=None,
) -> DeliveryAttempt:
    from .credentials import load_password
    from .transport import _default_smtp_factory, send_prepared_email

    current = DeliveryAttempt.objects.get(pk=attempt.pk)
    if current.result != DeliveryAttempt.Result.PREPARED:
        raise ValidationError("Only a prepared delivery attempt can be sent.")
    prepared = build_pdf_email(current, subject=subject, body=body)

    started = current

    def mark_started():
        nonlocal started
        started = mark_submission_started(current)

    result = send_prepared_email(
        prepared,
        smtp_config,
        credential_ref,
        on_submission_started=mark_started,
        password_loader=password_loader or load_password,
        smtp_factory=smtp_factory or _default_smtp_factory,
    )
    if result.success:
        return mark_accepted(started, code=result.code, message=result.message)
    if result.submission_started and result.code == "SMTP_RESULT_UNKNOWN":
        return mark_result_unknown(started, code=result.code, message=result.message)
    return mark_confirmed_failure(started, code=result.code, message=result.message)


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
        expected_results = (expected,) if isinstance(expected, str) else tuple(expected)
        if current.result not in expected_results:
            raise ValidationError(f"Invalid delivery transition; expected {expected}.")
        updated = Invoice.objects.filter(
            pk=current.invoice_id,
            status=Invoice.Status.IZBRAN_ZA_POSILJANJE,
        ).update(status=invoice_status)
        if updated != 1:
            raise ValidationError("Invoice status no longer permits this delivery transition.")
        DeliveryAttempt.objects.filter(
            pk=current.pk, result__in=expected_results
        ).update(
            result=result,
            result_code=code,
            message=message,
            completed_at=timezone.now(),
        )
    return DeliveryAttempt.objects.get(pk=attempt.pk)
