"""Explicit recipient review, preparation and cancellation for standalone ST."""

import json

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import connection, transaction
from django.utils import timezone

from core.models import AuditLogEntry
from invoicing.models import Invoice
from rendering.services import stanje_koncnega_pdf

from .execution import delivery_execution_lock
from .models import DeliveryAttempt


def require_st_invoice(invoice):
    if (
        invoice.pk is None or invoice._state.adding
        or invoice.document_type != Invoice.DocumentType.ST
        or not invoice.stevilka or not invoice.stevilka.strip()
        or invoice.lease_id is not None or invoice.povezan_racun_id is not None
        or invoice.status != Invoice.Status.IZBRAN_ZA_POSILJANJE
    ):
        raise ValidationError("Dostava zahteva oštevilčen samostojni ST, izbran za pošiljanje.")


def normalize_addresses(addresses):
    if not isinstance(addresses, (list, tuple)):
        raise ValidationError("Naslove vnesite kot seznam e-poštnih naslovov.")
    result, seen = [], set()
    for address in addresses:
        if not isinstance(address, str) or any(char in address for char in "\r\n\x00"):
            raise ValidationError("E-poštni naslov ni veljaven.")
        address = address.strip()
        try:
            validate_email(address)
        except ValidationError:
            raise ValidationError("E-poštni naslov ni veljaven.") from None
        local, domain = address.rsplit("@", 1)
        address = f"{local}@{domain.lower()}"
        if address.casefold() not in seen:
            result.append(address)
            seen.add(address.casefold())
    return result


def _snapshot(invoice, recipients_to, recipients_cc):
    require_st_invoice(invoice)
    latest = invoice.delivery_attempts.order_by("-sequence").first()
    if latest is not None and latest.result != DeliveryAttempt.Result.CANCELLED:
        raise ValidationError("Poskus dostave že obstaja. Uporabite dovoljeni ponovni poskus.")
    if invoice.rendered_eslog_path or invoice.rendered_eslog_checksum:
        raise ValidationError("Dostava ST podpira samo uradni PDF.")
    to = normalize_addresses(recipients_to)
    cc = normalize_addresses(recipients_cc)
    bcc = normalize_addresses([invoice.company.email_racunovodja])
    sender = invoice.company.email_posiljatelj.strip()
    normalize_addresses([sender])
    if not to:
        raise ValidationError("Vnesite vsaj en naslov TO.")
    # Keep the accountant hidden even if entered again in a visible role.
    # Ambiguous cross-role input must be corrected explicitly in the review.
    roles = [{address.casefold() for address in role} for role in (to, cc, bcc)]
    if roles[0] & roles[1] or roles[0] & roles[2] or roles[1] & roles[2]:
        raise ValidationError("Isti naslov ne sme nastopati v več vlogah TO, CC ali BCC.")
    return {
        "invoice_id": invoice.pk, "company_id": invoice.company_id,
        "number": invoice.stevilka, "sender": sender,
        "recipients_to": to, "recipients_cc": cc, "recipients_bcc": bcc,
        "pdf_path": invoice.rendered_pdf_path, "pdf_checksum": invoice.checksum,
        "latest_attempt": None if latest is None else {
            "id": latest.pk, "sequence": latest.sequence, "result": latest.result,
        },
    }


def review_st_delivery(invoice, *, recipients_to, recipients_cc=()):
    if connection.in_atomic_block:
        raise ValidationError("Pregled dostave mora potekati zunaj transakcije baze.")
    if invoice.pk is None or invoice._state.adding:
        raise ValidationError("Račun mora biti shranjen.")
    current = Invoice.objects.select_related("company").get(pk=invoice.pk)
    snapshot = _snapshot(current, recipients_to, recipients_cc)
    verify_st_pdf(current, snapshot)
    return snapshot


def verify_st_pdf(invoice, snapshot):
    if connection.in_atomic_block:
        raise ValidationError("Preverjanje PDF mora potekati zunaj transakcije baze.")
    state = stanje_koncnega_pdf(invoice)
    if state["state"] != "READY" or (
        state["result"].pot, state["result"].checksum
    ) != (snapshot["pdf_path"], snapshot["pdf_checksum"]):
        raise ValidationError("Celovitost registriranega uradnega PDF ni potrjena.")


def verify_st_retry_pdf(attempt):
    """Keep generic retry rules, with ST integrity checked before its transaction."""
    if attempt.pk is None:
        raise ValidationError("Poskus dostave mora biti shranjen.")
    current = DeliveryAttempt.objects.select_related("invoice").filter(pk=attempt.pk).first()
    if current is not None and current.invoice.document_type == Invoice.DocumentType.ST:
        verify_st_pdf(current.invoice, {
            "pdf_path": current.pdf_path, "pdf_checksum": current.pdf_checksum,
        })


def prepare_st_attempt(invoice, *, review):
    """Freeze exactly the reviewed recipients; preparation never sends mail."""
    if not isinstance(review, dict):
        raise ValidationError("Najprej preglejte prejemnike dostave.")
    verified = review_st_delivery(
        invoice, recipients_to=review.get("recipients_to"),
        recipients_cc=review.get("recipients_cc"),
    )
    if verified != review:
        raise ValidationError("Pregled dostave je zastarel. Ponovno preverite prejemnike.")
    with transaction.atomic():
        current = Invoice.objects.select_related("company").get(pk=invoice.pk)
        if _snapshot(current, verified["recipients_to"], verified["recipients_cc"]) != verified:
            raise ValidationError("Podatki dostave so se spremenili. Ponovite pregled.")
        attempt = DeliveryAttempt(
            invoice=current,
            sequence=1 if verified["latest_attempt"] is None else verified["latest_attempt"]["sequence"] + 1,
            **{name: verified[name] for name in (
                "sender", "recipients_to", "recipients_cc", "recipients_bcc",
                "pdf_path", "pdf_checksum",
            )},
        )
        attempt.full_clean()
        attempt.save()
        return attempt


def cancel_st_attempt(invoice, *, attempt_id, company_id, confirmed, reason):
    """Cancel under the SMTP execution lock; persist state and evidence together."""
    if connection.in_atomic_block:
        raise ValidationError("Preklic dostave mora potekati zunaj transakcije baze.")
    if confirmed is not True:
        raise ValidationError("Preklic morate izrecno potrditi.")
    if not isinstance(reason, str) or not reason.strip():
        raise ValidationError("Razlog za preklic je obvezen.")
    if invoice.pk is None or invoice._state.adding or not isinstance(attempt_id, int):
        raise ValidationError("Račun in poskus dostave morata biti shranjena.")
    with delivery_execution_lock(attempt_id) as acquired:
        if not acquired:
            raise ValidationError("Poskus dostave je v uporabi. Preklic ni dovoljen.")
        with transaction.atomic():
            current = Invoice.objects.filter(pk=invoice.pk, company_id=company_id).first()
            if current is None:
                raise ValidationError("Račun ne pripada izbranemu podjetju.")
            require_st_invoice(current)
            attempt = current.delivery_attempts.order_by("-sequence").first()
            if (
                attempt is None or attempt.pk != attempt_id
                or attempt.result != DeliveryAttempt.Result.PREPARED
                or attempt.submission_started_at is not None
                or attempt.execution_id is not None
            ):
                raise ValidationError("Prekličete lahko samo zadnji poskus pred začetkom oddaje.")
            completed_at = timezone.now()
            updated = DeliveryAttempt.objects.filter(
                pk=attempt.pk, invoice_id=current.pk,
                result=DeliveryAttempt.Result.PREPARED,
                submission_started_at__isnull=True, execution_id__isnull=True,
            ).update(result=DeliveryAttempt.Result.CANCELLED, completed_at=completed_at)
            if updated != 1:
                raise ValidationError("Stanje dostave se je spremenilo. Preklic ni dovoljen.")
            audit = AuditLogEntry(
                content_type=ContentType.objects.get_for_model(DeliveryAttempt),
                object_id=attempt.pk, dejanje=AuditLogEntry.Dejanje.SPREMEMBA,
                polje="result", stara_vrednost=DeliveryAttempt.Result.PREPARED,
                nova_vrednost=DeliveryAttempt.Result.CANCELLED,
                razlog=json.dumps({
                    "reason": reason.strip(), "invoice_id": current.pk,
                    "company_id": current.company_id, "number": current.stevilka,
                    "attempt_id": attempt.pk, "sequence": attempt.sequence,
                    "cancelled_at": completed_at.isoformat(),
                    **{name: getattr(attempt, name) for name in (
                        "sender", "recipients_to", "recipients_cc", "recipients_bcc",
                        "pdf_path", "pdf_checksum",
                    )},
                }, ensure_ascii=False),
            )
            audit.save()
        return DeliveryAttempt.objects.get(pk=attempt.pk)


def validate_st_send(attempt, smtp_config):
    from .transport import SmtpConfig

    invoice = attempt.invoice
    require_st_invoice(invoice)
    latest = invoice.delivery_attempts.order_by("-sequence").first()
    if latest is None or latest.pk != attempt.pk:
        raise ValidationError("Pošljete lahko samo zadnji pripravljeni poskus.")
    if (
        not isinstance(smtp_config, SmtpConfig)
        or smtp_config.username != attempt.sender
        or invoice.company.email_posiljatelj.strip() != attempt.sender
    ):
        raise ValidationError("Pošiljatelj SMTP se ne ujema z zamrznjenim pošiljateljem.")
