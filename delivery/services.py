import hashlib
import re
from copy import deepcopy
from dataclasses import dataclass
from email.message import EmailMessage
from uuid import uuid4

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import connection, transaction
from django.db.models import Max
from django.utils import timezone

from contracts.models import RecipientContact
from core.models import AuditLogEntry
from invoicing.models import Invoice, MonthlyCycle

from .execution import delivery_execution_lock
from .models import (
    AccountantReportDeliveryAttempt,
    DeliveryAttempt,
    MonthlySummaryAttempt,
)

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


@dataclass(frozen=True, slots=True)
class PreparedMonthlySummaryEmail:
    message: EmailMessage
    envelope_sender: str
    envelope_recipients: tuple[str, ...]


def has_sent_delivery_evidence(
    invoice: Invoice, attempt: DeliveryAttempt | None = None
) -> bool:
    if invoice.status != Invoice.Status.POSLAN:
        return False
    attempt = attempt or invoice.delivery_attempts.order_by("-sequence").first()
    if attempt is None:
        return False
    compatible = attempt.result == DeliveryAttempt.Result.ACCEPTED
    if attempt.result == DeliveryAttempt.Result.UNKNOWN:
        compatible = has_unknown_resolution(
            attempt, UNKNOWN_RESOLUTION_SENT_VERIFIED
        )
    return compatible and (attempt.pdf_path, attempt.pdf_checksum) == (
        invoice.rendered_pdf_path,
        invoice.checksum,
    )


def has_unknown_resolution(attempt: DeliveryAttempt, resolution: str) -> bool:
    content_type = ContentType.objects.filter(
        app_label=DeliveryAttempt._meta.app_label,
        model=DeliveryAttempt._meta.model_name,
    ).first()
    return content_type is not None and AuditLogEntry.objects.filter(
        content_type=content_type,
        object_id=attempt.pk,
        dejanje=AuditLogEntry.Dejanje.SPREMEMBA,
        polje=UNKNOWN_RESOLUTION_FIELD,
        stara_vrednost=UNKNOWN_RESOLUTION_UNRESOLVED,
        nova_vrednost=resolution,
    ).exists()


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
    if invoice.document_type in (Invoice.DocumentType.STORNO, Invoice.DocumentType.DOBROPIS):
        from .corrections import correction_email_content, validate_correction_attempt

        validate_correction_attempt(current)
        subject, body = correction_email_content(invoice)
    if invoice.document_type == Invoice.DocumentType.ST:
        from .st_delivery import require_st_invoice

        require_st_invoice(invoice)
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

    from .st_delivery import verify_st_retry_pdf

    verify_st_retry_pdf(failed_attempt)

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
    from .st_delivery import verify_st_retry_pdf

    verify_st_retry_pdf(attempt)
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
        execution_id=uuid4(),
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
    if connection.in_atomic_block:
        raise ValidationError("SMTP sending must run outside a DB transaction.")
    with delivery_execution_lock(attempt.pk) as acquired:
        if not acquired:
            raise ValidationError("Delivery attempt is already being sent.")
        return _send_pdf_email(
            attempt,
            subject=subject,
            body=body,
            smtp_config=smtp_config,
            credential_ref=credential_ref,
            password_loader=password_loader,
            smtp_factory=smtp_factory,
        )


def recover_interrupted_submissions(*, invoice_ids=None) -> int:
    """Reconcile unlocked submissions; stop pre-T5 senders before upgrading."""
    attempts = DeliveryAttempt.objects.filter(
        result=DeliveryAttempt.Result.SUBMISSION_STARTED,
    )
    if invoice_ids is not None:
        attempts = attempts.filter(invoice_id__in=invoice_ids)
    recovered = 0
    for attempt in attempts:
        with delivery_execution_lock(attempt.pk) as acquired:
            if not acquired:
                continue
            with transaction.atomic():
                current = DeliveryAttempt.objects.get(pk=attempt.pk)
                if (
                    current.result != DeliveryAttempt.Result.SUBMISSION_STARTED
                    or current.execution_id != attempt.execution_id
                ):
                    continue
                invoice = Invoice.objects.get(pk=current.invoice_id)
                latest = invoice.delivery_attempts.order_by("-sequence").first()
                if (
                    latest.pk != current.pk
                    or invoice.status != Invoice.Status.IZBRAN_ZA_POSILJANJE
                ):
                    continue
                message = (
                    "Izvajanje pošiljanja je prekinjeno brez shranjene končne potrditve. "
                    "Preverite mapo Poslano in izrecno potrdite rezultat. "
                    "Samodejno ponovno pošiljanje je blokirano."
                )
                mark_result_unknown(
                    current, code="SMTP_EXECUTION_INTERRUPTED", message=message
                )
                audit = AuditLogEntry(
                    content_type=ContentType.objects.get_for_model(DeliveryAttempt),
                    object_id=current.pk,
                    polje="result",
                    stara_vrednost=DeliveryAttempt.Result.SUBMISSION_STARTED,
                    nova_vrednost=DeliveryAttempt.Result.UNKNOWN,
                    razlog=(
                        (
                            f"Execution {current.execution_id}: "
                            if current.execution_id else
                            "Legacy attempt without execution identity "
                            "(pre-T5 senders must be stopped before upgrade): "
                        )
                        + "exclusive delivery lock acquired; no active sender owns this "
                        "attempt. No final result was persisted; "
                        "SMTP acceptance is unknown. No resend performed."
                    ),
                )
                audit.full_clean()
                audit.save()
                recovered += 1
    return recovered


def _send_pdf_email(
    attempt, *, subject, body, smtp_config, credential_ref,
    password_loader, smtp_factory,
) -> DeliveryAttempt:
    from .credentials import load_password
    from .transport import _default_smtp_factory, send_prepared_email

    current = DeliveryAttempt.objects.get(pk=attempt.pk)
    if current.result != DeliveryAttempt.Result.PREPARED:
        raise ValidationError("Only a prepared delivery attempt can be sent.")
    if current.invoice.document_type in (Invoice.DocumentType.STORNO, Invoice.DocumentType.DOBROPIS):
        from .corrections import validate_correction_send

        validate_correction_send(current, smtp_config)
    if current.invoice.document_type == Invoice.DocumentType.ST:
        from .st_delivery import validate_st_send

        validate_st_send(current, smtp_config)
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


def _monthly_summary_snapshot(cycle, final_state, prepared_at):
    issues = []
    for issue in final_state["issues"]:
        recipients = {"to": [], "cc": [], "bcc": []}
        if issue["invoice_id"] is not None:
            latest = (
                DeliveryAttempt.objects.filter(invoice_id=issue["invoice_id"])
                .order_by("-sequence")
                .first()
            )
            if latest is not None:
                recipients = {
                    "to": list(latest.recipients_to),
                    "cc": list(latest.recipients_cc),
                    "bcc": list(latest.recipients_bcc),
                }
        issues.append(
            {
                "company_id": issue["company_id"],
                "issuer_label": issue["issuer_label"],
                "invoice_id": issue["invoice_id"],
                "invoice_number": issue["invoice_number"],
                "category": issue["category"],
                "message": issue["message"],
                "delivery_result_code": issue["delivery_result_code"],
                "delivery_result_message": issue["delivery_result_message"],
                "recipients": recipients,
            }
        )
    return {
        "cycle_id": cycle.pk,
        "period": final_state["period"].isoformat(),
        "prepared_at": prepared_at.isoformat(),
        "headline": final_state["headline"],
        "requires_attention": final_state["requires_attention"],
        "counts": {
            "prepared": final_state["prepared_count"],
            "sent": final_state["sent_count"],
            "intentionally_unissued": final_state["intentionally_unissued_count"],
            "failed": final_state["failed_count"],
            "unknown": final_state["unknown_count"],
        },
        "archive": {
            "complete": final_state["archive_complete"],
            "verified_count": final_state["archive_verified_count"],
            "pending_count": final_state["archive_pending_count"],
        },
        "per_company": deepcopy(final_state["per_company"]),
        "issues": issues,
    }


def _monthly_summary_body(snapshot):
    counts = snapshot["counts"]
    archive = snapshot["archive"]
    lines = [
        snapshot["headline"],
        "",
        f"Obračunski mesec: {snapshot['period'][5:7]}/{snapshot['period'][:4]}",
        f"Čas priprave poročila: {snapshot['prepared_at']}",
        f"Pripravljeni računi: {counts['prepared']}",
        f"Uspešno poslani računi: {counts['sent']}",
        f"Namenoma neizdani računi: {counts['intentionally_unissued']}",
        f"Neuspešni računi: {counts['failed']}",
        f"Računi z neznanim rezultatom: {counts['unknown']}",
        f"Arhiv: {'zaključen' if archive['complete'] else 'čaka'}",
    ]
    for company in snapshot["per_company"]:
        lines.extend(
            [
                "",
                f"Podjetje: {company['issuer_label']}",
                f"Pripravljeni: {company['prepared_count']}",
                f"Poslani: {company['sent_count']}",
                f"Namenoma neizdani: {company['intentionally_unissued_count']}",
                f"Neuspešni: {company['failed_count']}",
                f"Neznani: {company['unknown_count']}",
                f"Arhivirani: {company['archive_verified_count']}",
            ]
        )
    if snapshot["issues"]:
        lines.extend(["", "Težave:"])
        for issue in snapshot["issues"]:
            recipients = [
                *issue["recipients"]["to"],
                *issue["recipients"]["cc"],
                *issue["recipients"]["bcc"],
            ]
            reason = issue["delivery_result_message"] or issue["message"]
            if issue["delivery_result_code"]:
                reason = f"{issue['delivery_result_code']}: {reason}"
            lines.append(
                f"- {issue['issuer_label']}; račun {issue['invoice_number'] or '-'}; "
                f"prejemniki {', '.join(recipients) or '-'}; razlog: {reason}"
            )
    return "\n".join(lines) + "\n"


def prepare_monthly_summary(cycle, configuration) -> MonthlySummaryAttempt:
    from invoicing.services import stanje_zakljucne_kontrole_mesecnega_cikla

    from .configuration import resolve_monthly_summary_configuration

    if cycle.pk is None:
        raise ValidationError("Cycle must be saved.")
    configuration = resolve_monthly_summary_configuration(configuration)
    final_state = stanje_zakljucne_kontrole_mesecnega_cikla(cycle)
    if final_state["delivery_pending_count"] > 0:
        raise ValidationError("Monthly summary cannot be prepared while invoice delivery is pending.")
    prepared_at = timezone.localtime(timezone.now())
    snapshot = _monthly_summary_snapshot(cycle, final_state, prepared_at)
    subject = f"Mesečni postopek – {cycle.obdobje:%m/%Y}"
    body = _monthly_summary_body(snapshot)
    with transaction.atomic():
        current_cycle = MonthlyCycle.objects.get(pk=cycle.pk)
        _require_active_step_6(current_cycle)
        latest = (
            MonthlySummaryAttempt.objects.filter(cycle=current_cycle)
            .order_by("-sequence")
            .first()
        )
        if latest is not None:
            if latest.result == MonthlySummaryAttempt.Result.ACCEPTED:
                raise ValidationError("Monthly summary was already accepted.")
            raise ValidationError("A monthly summary attempt already exists; use the retry service when allowed.")
        return MonthlySummaryAttempt.objects.create(
            cycle=current_cycle,
            sequence=1,
            sender=configuration.email,
            recipient=configuration.email,
            subject=subject,
            body=body,
            summary_snapshot=snapshot,
        )


def prepare_monthly_summary_retry(failed_attempt) -> MonthlySummaryAttempt:
    if failed_attempt.pk is None:
        raise ValidationError("Monthly summary attempt must be saved.")
    with transaction.atomic():
        current = MonthlySummaryAttempt.objects.select_related("cycle").get(
            pk=failed_attempt.pk
        )
        _require_active_step_6(current.cycle)
        latest = (
            MonthlySummaryAttempt.objects.filter(cycle_id=current.cycle_id)
            .order_by("-sequence")
            .first()
        )
        if latest.pk != current.pk or current.result != MonthlySummaryAttempt.Result.CONFIRMED_FAILURE:
            raise ValidationError("Only the latest confirmed failure can be retried.")
        return MonthlySummaryAttempt.objects.create(
            cycle_id=current.cycle_id,
            sequence=current.sequence + 1,
            sender=current.sender,
            recipient=current.recipient,
            subject=current.subject,
            body=current.body,
            summary_snapshot=deepcopy(current.summary_snapshot),
        )


def build_monthly_summary_email(attempt) -> PreparedMonthlySummaryEmail:
    if attempt.pk is None:
        raise ValidationError("Monthly summary attempt must be saved.")
    current = MonthlySummaryAttempt.objects.get(pk=attempt.pk)
    if current.result != MonthlySummaryAttempt.Result.PREPARED:
        raise ValidationError("Only a prepared monthly summary can be composed.")
    message = EmailMessage()
    message["From"] = current.sender
    message["To"] = current.recipient
    message["Subject"] = current.subject
    message.set_content(current.body)
    return PreparedMonthlySummaryEmail(message, current.sender, (current.recipient,))


def _transition_monthly_summary(attempt, *, expected, result, **values):
    updated = MonthlySummaryAttempt.objects.filter(pk=attempt.pk, result=expected).update(
        result=result, **values
    )
    if updated != 1:
        raise ValidationError(f"Invalid monthly summary transition; expected {expected}.")
    return MonthlySummaryAttempt.objects.get(pk=attempt.pk)


def send_monthly_summary(
    attempt,
    smtp_config,
    credential_ref,
    *,
    password_loader=None,
    smtp_factory=None,
):
    from .credentials import load_password
    from .transport import SmtpConfig, _default_smtp_factory, send_prepared_email

    current = MonthlySummaryAttempt.objects.get(pk=attempt.pk)
    if not isinstance(smtp_config, SmtpConfig):
        raise ValidationError("A valid SMTP configuration is required.")
    if current.sender != current.recipient:
        raise ValidationError("Monthly summary sender and recipient must match.")
    if smtp_config.username != current.sender:
        raise ValidationError("SMTP username must match the monthly summary sender.")
    prepared = build_monthly_summary_email(current)
    started = current

    def mark_started():
        nonlocal started
        started = _transition_monthly_summary(
            current,
            expected=MonthlySummaryAttempt.Result.PREPARED,
            result=MonthlySummaryAttempt.Result.SUBMISSION_STARTED,
            submission_started_at=timezone.now(),
        )

    result = send_prepared_email(
        prepared,
        smtp_config,
        credential_ref,
        on_submission_started=mark_started,
        password_loader=password_loader or load_password,
        smtp_factory=smtp_factory or _default_smtp_factory,
    )
    code, message = _sanitize(result.code, result.message)
    if result.success:
        final = MonthlySummaryAttempt.Result.ACCEPTED
    elif result.submission_started and result.code == "SMTP_RESULT_UNKNOWN":
        final = MonthlySummaryAttempt.Result.UNKNOWN
    else:
        final = MonthlySummaryAttempt.Result.CONFIRMED_FAILURE
    return _transition_monthly_summary(
        started,
        expected=started.result,
        result=final,
        result_code=code,
        message=message,
        completed_at=timezone.now(),
    )


def _require_active_step_6(cycle):
    if (
        cycle.status != MonthlyCycle.Status.V_TEKU
        or cycle.trenutni_korak != MonthlyCycle.Korak.ZAKLJUCEK
    ):
        raise ValidationError("Monthly cycle must remain active in Step 6.")


_SLOVENIAN_MONTHS = (
    "januar", "februar", "marec", "april", "maj", "junij", "julij", "avgust",
    "september", "oktober", "november", "december",
)
_ACCOUNTANT_FROZEN_FIELDS = (
    "sender", "recipient", "subject", "body", "attachment_filename", "attachment_checksum",
)


@dataclass(frozen=True, slots=True)
class PreparedAccountantReportEmail:
    message: EmailMessage
    envelope_sender: str
    envelope_recipients: tuple[str, ...]


def _accountant_outside_transaction():
    if connection.in_atomic_block:
        raise ValidationError("Accountant delivery preparation and sending must run outside a DB transaction.")


def _accountant_persisted(instance):
    if instance.pk is None or instance._state.adding:
        raise ValidationError("Accountant report or delivery attempt must be persisted.")


def _accountant_report(report_id):
    from accounting_export.models import AccountantReport

    return AccountantReport.objects.only("company_id", "snapshot").get(pk=report_id)


def _verify_accountant_pdf(report, local_root):
    from accounting_export.archive import verify_accountant_report_pdf_artifact
    from accounting_export.models import AccountantReportPdfArtifact

    artifact = AccountantReportPdfArtifact.objects.filter(report_id=report.pk).first()
    if artifact is None:
        raise ValidationError("A fixed local accountant PDF artifact is required.")
    verified = verify_accountant_report_pdf_artifact(artifact, local_root)
    if not verified.success:
        raise ValidationError(f"Fixed local accountant PDF is not verified: {verified.state}.")


def _accountant_attachment(report, attempt=None):
    from accounting_export.csv_renderer import (
        accountant_report_csv_filename,
        render_accountant_report_csv,
    )

    data = render_accountant_report_csv(report)
    filename = accountant_report_csv_filename(report)
    checksum = hashlib.sha256(data).hexdigest()
    if attempt is not None and (filename, checksum) != (
        attempt.attachment_filename, attempt.attachment_checksum,
    ):
        raise ValidationError("Accountant attachment differs from frozen filename or checksum.")
    return data, filename, checksum


def _accountant_identity_unchanged(report):
    current = _accountant_report(report.pk)
    if current.company_id != report.company_id or current.snapshot != report.snapshot:
        raise ValidationError("Accountant report identity changed during preparation.")
    return current


def prepare_accountant_report_delivery(report, *, local_root) -> AccountantReportDeliveryAttempt:
    from companies.models import Company

    _accountant_outside_transaction()
    _accountant_persisted(report)
    persisted = _accountant_report(report.pk)
    if AccountantReportDeliveryAttempt.objects.filter(report_id=persisted.pk).exists():
        raise ValidationError("An accountant delivery attempt already exists; use the retry service when allowed.")
    company = Company.objects.only("email_posiljatelj", "email_racunovodja").get(pk=persisted.company_id)
    sender = _required_text(company.email_posiljatelj, "Sender")
    recipient = _required_text(company.email_racunovodja, "Recipient")
    validate_email(sender)
    validate_email(recipient)
    _verify_accountant_pdf(persisted, local_root)
    _, filename, checksum = _accountant_attachment(persisted)
    header = persisted.snapshot["header"]
    year, month = header["period"].split("-")
    period = f"{_SLOVENIAN_MONTHS[int(month) - 1]} - {year}"
    subject = f"Kontrolni dokument za {header['company_name']} - {period}"
    if len(subject) > 255 or "\r" in subject or "\n" in subject:
        raise ValidationError("Invalid accountant email subject.")
    body = f"V prilogi je kontrolni dokument za {header['company_name']} - {period}.\n"
    with transaction.atomic():
        current = _accountant_identity_unchanged(persisted)
        current_company = Company.objects.only("email_posiljatelj", "email_racunovodja").get(pk=current.company_id)
        if (current_company.email_posiljatelj, current_company.email_racunovodja) != (
            company.email_posiljatelj, company.email_racunovodja,
        ):
            raise ValidationError("Company delivery settings changed during preparation.")
        if AccountantReportDeliveryAttempt.objects.filter(report_id=current.pk).exists():
            raise ValidationError("An accountant delivery attempt already exists.")
        return AccountantReportDeliveryAttempt.objects.create(
            report_id=current.pk, sequence=1, sender=sender, recipient=recipient,
            subject=subject, body=body, attachment_filename=filename, attachment_checksum=checksum,
        )


def _require_accountant_retry(source):
    latest = AccountantReportDeliveryAttempt.objects.filter(report_id=source.report_id).order_by("-sequence").first()
    if latest is None or latest.pk != source.pk or source.result != AccountantReportDeliveryAttempt.Result.CONFIRMED_FAILURE:
        raise ValidationError("Only the latest confirmed accountant delivery failure can be retried.")


def prepare_accountant_report_delivery_retry(failed_attempt, *, local_root) -> AccountantReportDeliveryAttempt:
    _accountant_outside_transaction()
    _accountant_persisted(failed_attempt)
    source = AccountantReportDeliveryAttempt.objects.get(pk=failed_attempt.pk)
    _require_accountant_retry(source)
    report = _accountant_report(source.report_id)
    _verify_accountant_pdf(report, local_root)
    _accountant_attachment(report, source)
    frozen = {field: getattr(source, field) for field in _ACCOUNTANT_FROZEN_FIELDS}
    with transaction.atomic():
        _accountant_identity_unchanged(report)
        current = AccountantReportDeliveryAttempt.objects.get(pk=source.pk)
        _require_accountant_retry(current)
        if current.report_id != source.report_id or current.sequence != source.sequence or any(
            getattr(current, field) != value for field, value in frozen.items()
        ):
            raise ValidationError("Frozen accountant delivery metadata changed during retry preparation.")
        return AccountantReportDeliveryAttempt.objects.create(
            report_id=source.report_id, sequence=source.sequence + 1, **frozen,
        )


def build_accountant_report_email(attempt) -> PreparedAccountantReportEmail:
    _accountant_outside_transaction()
    _accountant_persisted(attempt)
    current = AccountantReportDeliveryAttempt.objects.get(pk=attempt.pk)
    if current.result != AccountantReportDeliveryAttempt.Result.PREPARED:
        raise ValidationError("Only a prepared accountant delivery attempt can be composed.")
    validate_email(current.sender)
    validate_email(current.recipient)
    data, filename, _ = _accountant_attachment(_accountant_report(current.report_id), current)
    message = EmailMessage()
    message["From"] = current.sender
    message["To"] = current.recipient
    message["Subject"] = current.subject
    message.set_content(current.body)
    message.add_attachment(data, maintype="text", subtype="csv", filename=filename)
    return PreparedAccountantReportEmail(message, current.sender, (current.recipient,))


def _transition_accountant_delivery(attempt, *, expected, result, **values):
    updated = AccountantReportDeliveryAttempt.objects.filter(pk=attempt.pk, result=expected).update(
        result=result, **values,
    )
    if updated != 1:
        raise ValidationError(f"Invalid accountant delivery transition; expected {expected}.")
    return AccountantReportDeliveryAttempt.objects.get(pk=attempt.pk)


def send_accountant_report(
    attempt, smtp_config, credential_ref, *, password_loader=None, smtp_factory=None,
) -> AccountantReportDeliveryAttempt:
    from .credentials import load_password
    from .transport import SmtpConfig, _default_smtp_factory, send_prepared_email

    _accountant_outside_transaction()
    _accountant_persisted(attempt)
    current = AccountantReportDeliveryAttempt.objects.get(pk=attempt.pk)
    if not isinstance(smtp_config, SmtpConfig):
        raise ValidationError("A valid SMTP configuration is required.")
    if smtp_config.username != current.sender:
        raise ValidationError("SMTP username must match the frozen accountant delivery sender.")
    prepared = build_accountant_report_email(current)
    started = current

    def mark_started():
        nonlocal started
        started = _transition_accountant_delivery(
            current, expected=AccountantReportDeliveryAttempt.Result.PREPARED,
            result=AccountantReportDeliveryAttempt.Result.SUBMISSION_STARTED,
            submission_started_at=timezone.now(),
        )

    result = send_prepared_email(
        prepared, smtp_config, credential_ref, on_submission_started=mark_started,
        password_loader=password_loader or load_password,
        smtp_factory=smtp_factory or _default_smtp_factory,
    )
    code, message = _sanitize(result.code, result.message)
    if result.success:
        final = AccountantReportDeliveryAttempt.Result.ACCEPTED
        expected = AccountantReportDeliveryAttempt.Result.SUBMISSION_STARTED
    elif result.submission_started and result.code == "SMTP_RESULT_UNKNOWN":
        final = AccountantReportDeliveryAttempt.Result.UNKNOWN
        expected = AccountantReportDeliveryAttempt.Result.SUBMISSION_STARTED
    else:
        final = AccountantReportDeliveryAttempt.Result.CONFIRMED_FAILURE
        expected = started.result
    return _transition_accountant_delivery(
        started, expected=expected, result=final, result_code=code, message=message,
        completed_at=timezone.now(),
    )


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
