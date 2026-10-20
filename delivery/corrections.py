"""Explicit, PDF-only delivery of issued corrections, with immutable evidence."""

import hashlib
import json
from collections import Counter

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import connection, transaction

from core.models import AuditLogEntry
from invoicing.corrections import (
    credit_source,
    standalone_original,
    validate_st_correction_pdf,
)
from invoicing.models import Invoice
from invoicing.services import (
    _SNAPSHOT_POLJA_KOREKCIJE,
    _kljuc_storno_vrstice,
    _preveri_izvirnik_korekcije,
    _preveri_rocno_vrstico_dobropisa,
    _storno_vrstica,
)
from rendering.contexts import preveri_pdf_predpogoje

from .models import DeliveryAttempt
from .st_delivery import normalize_addresses, verify_st_pdf

TYPES = (Invoice.DocumentType.STORNO, Invoice.DocumentType.DOBROPIS)
EVIDENCE_FIELD = "correction_delivery_v1"
FROZEN = ("sender", "recipients_to", "recipients_cc", "recipients_bcc", "pdf_path", "pdf_checksum")


def _outside_transaction():
    if connection.in_atomic_block or not connection.get_autocommit():
        raise ValidationError("Operacija dostave mora potekati zunaj transakcije baze.")


def require_correction(invoice):
    if (invoice.pk is None or invoice._state.adding or invoice.document_type not in TYPES
            or not invoice.stevilka or not invoice.stevilka.strip()
            or invoice.status not in (Invoice.Status.IZBRAN_ZA_POSILJANJE, Invoice.Status.POSLAN,
                                      Invoice.Status.NAPAKA, Invoice.Status.REZULTAT_NI_ZNAN)
            or not invoice.povezan_racun_id or not invoice.razlog.strip()
            or invoice.rendered_eslog_path or invoice.rendered_eslog_checksum):
        raise ValidationError("Dostava zahteva izdani storno ali dobropis brez eSLOG.")
    original = _preveri_izvirnik_korekcije(invoice.povezan_racun)
    if (invoice.company_id != original.company_id or invoice.lease_id != original.lease_id
            or invoice.obdobje_od != original.obdobje_od or invoice.obdobje_do != original.obdobje_do
            or any(getattr(invoice, name) != getattr(original, name) for name in _SNAPSHOT_POLJA_KOREKCIJE)):
        raise ValidationError("Povezava in posnetki korekcije se ne ujemajo z izvirnikom.")
    lines = list(invoice.invoiceline_set.order_by("pk"))
    if not lines:
        raise ValidationError("Korekcija nima postavk.")
    if invoice.document_type == Invoice.DocumentType.STORNO:
        expected = Counter(_kljuc_storno_vrstice(_storno_vrstica(line, invoice))
                           for line in original.invoiceline_set.all())
        if expected != Counter(_kljuc_storno_vrstice(line) for line in lines):
            raise ValidationError("Posnetki postavk storna niso skladni z izvirnikom.")
    else:
        for line in lines:
            credit_source(line)
            _preveri_rocno_vrstico_dobropisa(line)
    if standalone_original(original):
        validate_st_correction_pdf(invoice, lines)
    return original


def _identity(invoice):
    original = require_correction(invoice)
    def values(record, exclude=()):
        return {field.attname: getattr(record, field.attname) for field in record._meta.concrete_fields
                if field.name != "status" and field.name not in exclude}
    payload = {
        "invoice": values(invoice),
        # Original delivery/rendering progresses independently of its correction.
        "original": values(original, ("rendered_pdf_path", "checksum", "rendered_eslog_path", "rendered_eslog_checksum")),
        "lines": list(invoice.invoiceline_set.order_by("pk").values()),
        "original_lines": list(original.invoiceline_set.order_by("pk").values()),
    }
    return hashlib.sha256(json.dumps(payload, cls=DjangoJSONEncoder, sort_keys=True).encode()).hexdigest()


def _recipients(to, cc, bcc):
    roles = [normalize_addresses(addresses) for addresses in (to, cc, bcc)]
    if not roles[0] or not roles[2]:
        raise ValidationError("Obvezna sta vsaj en naslov TO in skriti naslov računovodje.")
    sets = [{address.casefold() for address in role} for role in roles]
    if sets[0] & sets[1] or sets[0] & sets[2] or sets[1] & sets[2]:
        raise ValidationError("Isti naslov ne sme nastopati v več vlogah TO, CC ali BCC.")
    return dict(zip(("recipients_to", "recipients_cc", "recipients_bcc"), roles))


def _snapshot(invoice, to, cc):
    identity = _identity(invoice)
    if invoice.status != Invoice.Status.IZBRAN_ZA_POSILJANJE or invoice.delivery_attempts.exists():
        raise ValidationError("Uporabite dovoljeno dejanje za zadnji poskus dostave.")
    sender = invoice.company.email_posiljatelj.strip()
    normalize_addresses([sender])
    return {
        "invoice_id": invoice.pk, "company_id": invoice.company_id,
        "number": invoice.stevilka, "document_type": invoice.document_type,
        "original_id": invoice.povezan_racun_id,
        "original_number": invoice.povezan_racun.stevilka,
        "original_type": invoice.povezan_racun.document_type,
        "identity": identity, "sender": sender,
        **_recipients(to, cc, [invoice.company.email_racunovodja]),
        "pdf_path": invoice.rendered_pdf_path, "pdf_checksum": invoice.checksum,
    }


def review_correction_delivery(invoice, *, recipients_to, recipients_cc=()):
    _outside_transaction()
    current = Invoice.objects.select_related("company", "povezan_racun").get(pk=invoice.pk)
    snapshot = _snapshot(current, recipients_to, recipients_cc)
    preveri_pdf_predpogoje(current)
    verify_st_pdf(current, snapshot)
    return snapshot


def _record(attempt, identity, *, reason):
    audit = AuditLogEntry(
        content_type=ContentType.objects.get_for_model(DeliveryAttempt), object_id=attempt.pk,
        polje=EVIDENCE_FIELD, nova_vrednost=json.dumps({
            "identity": identity, "invoice_id": attempt.invoice_id,
            "sequence": attempt.sequence, **{name: getattr(attempt, name) for name in FROZEN},
        }, sort_keys=True), razlog=reason,
    )
    audit.full_clean()
    audit.save()


def prepare_correction_attempt(invoice, *, review):
    if not isinstance(review, dict):
        raise ValidationError("Najprej preglejte prejemnike.")
    verified = review_correction_delivery(invoice, recipients_to=review.get("recipients_to"),
                                         recipients_cc=review.get("recipients_cc"))
    if verified != review:
        raise ValidationError("Pregled dostave je zastarel. Ponovite pregled.")
    with transaction.atomic():
        current = Invoice.objects.select_related("company", "povezan_racun").get(pk=invoice.pk)
        if _snapshot(current, verified["recipients_to"], verified["recipients_cc"]) != verified:
            raise ValidationError("Pregled dostave je zastarel. Ponovite pregled.")
        attempt = DeliveryAttempt(invoice=current, sequence=1, **{name: verified[name] for name in FROZEN})
        attempt.full_clean()
        attempt.save()
        _record(attempt, verified["identity"], reason="Uporabnik je izrecno pregledal in potrdil dostavo korekcije.")
    return attempt


def validate_correction_attempt(attempt, *, verify_pdf=True):
    current = DeliveryAttempt.objects.select_related("invoice__company", "invoice__povezan_racun").get(pk=attempt.pk)
    invoice = current.invoice
    identity = _identity(invoice)
    latest = invoice.delivery_attempts.order_by("-sequence").first()
    if latest.pk != current.pk:
        raise ValidationError("Dovoljen je samo zadnji poskus dostave.")
    _recipients(current.recipients_to, current.recipients_cc, current.recipients_bcc)
    normalize_addresses([current.sender])
    try:
        evidence = json.loads(AuditLogEntry.objects.get(
            content_type=ContentType.objects.get_for_model(DeliveryAttempt),
            object_id=current.pk, polje=EVIDENCE_FIELD,
        ).nova_vrednost)
    except (AuditLogEntry.DoesNotExist, AuditLogEntry.MultipleObjectsReturned, ValueError, TypeError):
        raise ValidationError("Nespremenljivo dokazilo dostave manjka ali ni veljavno.") from None
    expected = {"identity": identity, "invoice_id": invoice.pk, "sequence": current.sequence,
                **{name: getattr(current, name) for name in FROZEN}}
    if evidence != expected or (current.pdf_path, current.pdf_checksum) != (invoice.rendered_pdf_path, invoice.checksum):
        raise ValidationError("Dokazilo dostave se ne ujema s korekcijo ali zamrznjenimi podatki.")
    if verify_pdf:
        _outside_transaction()
        preveri_pdf_predpogoje(invoice)
        verify_st_pdf(invoice, expected)
    return current


def validate_correction_send(attempt, smtp_config):
    from .transport import SmtpConfig
    current = validate_correction_attempt(attempt)
    if (current.invoice.status != Invoice.Status.IZBRAN_ZA_POSILJANJE
            or not isinstance(smtp_config, SmtpConfig) or smtp_config.username != current.sender
            or current.invoice.company.email_posiljatelj.strip() != current.sender):
        raise ValidationError("Stanje korekcije ali pošiljatelj SMTP se ne ujema s pripravljenim poskusom.")


def correction_email_content(invoice):
    label = invoice.get_document_type_display()
    return (f"{label} {invoice.stevilka}",
            (f"V prilogi je {label.lower()} {invoice.stevilka} za izvirni dokument "
             f"{invoice.povezan_racun.document_type} {invoice.povezan_racun.stevilka}.\n"))


def send_correction_email(attempt, **transport):
    from .services import send_pdf_email
    current = validate_correction_attempt(attempt)
    subject, body = correction_email_content(current.invoice)
    return send_pdf_email(current, subject=subject, body=body, **transport)


def _reason(reason):
    if not isinstance(reason, str) or not any(char.isalpha() for char in reason):
        raise ValidationError("Vpišite smiseln razlog in ugotovitev preverjanja.")
    return reason.strip()


def correction_delivery_action(attempt, *, action, reason=""):
    from .services import (
        prepare_retry_attempt,
        resolve_unknown_as_sent,
        resolve_unknown_user_confirmed_not_sent_and_prepare_retry,
    )
    _outside_transaction()
    current = validate_correction_attempt(attempt)
    reason = _reason(reason)
    with transaction.atomic():
        current = validate_correction_attempt(current, verify_pdf=False)
        if action == "retry":
            result = prepare_retry_attempt(current)
        elif action == "unknown_sent":
            return resolve_unknown_as_sent(current, reason=reason)
        elif action == "unknown_not_sent":
            result = resolve_unknown_user_confirmed_not_sent_and_prepare_retry(current, reason=reason)
        else:
            raise ValidationError("Neznano dejanje dostave.")
        _record(result, _identity(result.invoice), reason=reason)
        if action == "retry":
            AuditLogEntry.objects.create(
                content_type=ContentType.objects.get_for_model(DeliveryAttempt), object_id=current.pk,
                polje="correction_retry", stara_vrednost=str(current.pk), nova_vrednost=str(result.pk), razlog=reason,
            )
    return result
