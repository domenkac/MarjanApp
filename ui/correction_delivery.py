"""Correction delivery endpoints; every mutation requires an explicit POST."""

from django import forms
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import override
from django.views.decorators.http import require_http_methods, require_POST

from archive.corrections import archive_correction, correction_archive_status
from delivery.configuration import resolve_keyring_credential_ref, resolve_smtp_config
from delivery.corrections import (
    correction_delivery_action,
    prepare_correction_attempt,
    review_correction_delivery,
    send_correction_email,
    validate_correction_attempt,
)
from delivery.models import DeliveryAttempt
from delivery.services import (
    has_sent_delivery_evidence,
    recover_interrupted_submissions,
)
from rendering.contexts import PdfRenderNapaka

from .corrections import _issued
from .st_delivery import _FAILURES, _RESULTS, STDeliveryConfirmForm, STRecipientsForm

_SALT = "correction-delivery-review-v1"
_ERROR = (
    "Dejanje je blokirano ali rezultat ni shranjen. Preverite zadnji poskus, podatke korekcije, "
    "prejemnike, nastavitve SMTP in celovitost PDF. Ob spremembi podatkov ponovite pregled. "
    "Za ponovitev ali razrešitev neznanega rezultata vnesite smiseln razlog."
)


class CorrectionDeliveryConfirmForm(STDeliveryConfirmForm):
    confirm_recipients = forms.BooleanField(
        label="Potrjujem pošiljatelja, TO, CC, skriti BCC računovodji, številko in tip korekcije, izvirnik ter SHA-256 uradnega PDF.",
    )


def delivery_context(invoice):
    history = list(invoice.delivery_attempts.order_by("-sequence"))
    attempt = history[0] if history else None
    result = _RESULTS.get(attempt.result, "") if attempt else "Dostava še ni pripravljena."
    if attempt and attempt.result == "ACCEPTED":
        result = "Odhodni strežnik je sporočilo sprejel. Korekcija je poslana."
    if attempt and attempt.result == "UNKNOWN" and has_sent_delivery_evidence(invoice, attempt):
        result = "Uporabnik je potrdil preverjeno pošiljanje. Izvirni rezultat UNKNOWN je ohranjen."
    failure = ""
    if attempt and attempt.result in {"UNKNOWN", "CONFIRMED_FAILURE"}:
        failure = _FAILURES.get(attempt.result_code, "Rezultat oddaje ni zanesljivo potrjen. Preverite dokazila pošiljanja.")
    return {
        "invoice": invoice, "attempt": attempt, "history": history, "result_label": result,
        "failure_label": failure, "can_prepare": invoice.status == "IZBRAN_ZA_POSILJANJE" and not history,
        "archive_state": correction_archive_status(invoice, company_id=invoice.company_id),
        "active_section": "corrections", "active_page": "corrections",
    }


@transaction.non_atomic_requests
@require_http_methods(["GET", "POST"])
@override("sl")
def correction_delivery(request, company_id, invoice_id):
    invoice = _issued(company_id, invoice_id)
    context = delivery_context(invoice)
    # Historical contacts are suggestions only; no address is accepted without review.
    tenant = invoice.povezan_racun.lease.tenant if invoice.povezan_racun.lease_id else invoice.povezan_racun.prejemnik
    suggestion = tenant.email if tenant else ""
    form = STRecipientsForm(request.POST if request.method == "POST" else None,
                            initial={"recipients_to": suggestion})
    if request.method == "POST" and form.is_valid():
        try:
            review = review_correction_delivery(invoice, **form.cleaned_data)
        except (ValidationError, PdfRenderNapaka, ValueError, OSError, DatabaseError):
            context["action_error"] = _ERROR
        else:
            context.update(review=review, confirm_form=CorrectionDeliveryConfirmForm(initial={
                "review_token": signing.dumps(review, salt=_SALT),
            }))
    context.update(form=form, suggestion=bool(suggestion))
    return render(request, "ui/correction_delivery.html", context)


@transaction.non_atomic_requests
@require_POST
@override("sl")
def correction_prepare_delivery(request, company_id, invoice_id):
    invoice = _issued(company_id, invoice_id)
    form = CorrectionDeliveryConfirmForm(request.POST)
    if form.is_valid():
        try:
            review = signing.loads(form.cleaned_data["review_token"], salt=_SALT)
            prepare_correction_attempt(invoice, review=review)
        except (signing.BadSignature, ValidationError, ValueError, OSError, DatabaseError):
            pass
        else:
            return redirect("ui:correction_delivery", company_id=company_id, invoice_id=invoice_id)
    context = delivery_context(invoice)
    context.update(form=STRecipientsForm(), action_error=_ERROR)
    return render(request, "ui/correction_delivery.html", context, status=409)


@transaction.non_atomic_requests
@require_POST
@override("sl")
def correction_attempt_action(request, company_id, invoice_id, attempt_id):
    invoice = _issued(company_id, invoice_id)
    attempt = get_object_or_404(DeliveryAttempt, pk=attempt_id, invoice_id=invoice.pk)
    action = request.POST.get("action")
    try:
        if action == "send":
            send_correction_email(attempt, smtp_config=resolve_smtp_config(invoice.company),
                                  credential_ref=resolve_keyring_credential_ref(invoice.company))
        elif action == "recover":
            validate_correction_attempt(attempt)
            if attempt.result != "SUBMISSION_STARTED":
                raise ValidationError("Oddaja ni v teku.")
            recover_interrupted_submissions(invoice_ids=[invoice.pk])
        else:
            correction_delivery_action(attempt, action=action, reason=request.POST.get("reason", ""))
    except Exception:  # noqa: BLE001 -- Fail closed without exposing operational secrets.
        invoice.refresh_from_db()
        context = delivery_context(invoice)
        context.update(form=STRecipientsForm(), action_error=_ERROR)
        return render(request, "ui/correction_delivery.html", context, status=409)
    return redirect("ui:correction_delivery", company_id=company_id, invoice_id=invoice_id)


@transaction.non_atomic_requests
@require_POST
@override("sl")
def correction_archive(request, company_id, invoice_id):
    invoice = _issued(company_id, invoice_id)
    state = archive_correction(invoice, company_id=company_id)
    if state.verified:
        return redirect("ui:correction_detail", company_id=company_id, invoice_id=invoice_id)
    context = delivery_context(invoice)
    context.update(form=STRecipientsForm(), archive_state=state, action_error=state.message)
    return render(request, "ui/correction_delivery.html", context, status=409)
