"""Standalone ST delivery actions; GET is read-only and SMTP is non-atomic."""

from django import forms
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import override
from django.views.decorators.http import require_http_methods, require_POST

from delivery.configuration import (
    resolve_keyring_credential_ref,
    resolve_smtp_config,
)
from delivery.models import DeliveryAttempt
from delivery.services import (
    has_sent_delivery_evidence,
    prepare_retry_attempt,
    recover_interrupted_submissions,
    resolve_unknown_as_sent,
    resolve_unknown_user_confirmed_not_sent_and_prepare_retry,
    send_pdf_email,
)
from delivery.st_delivery import (
    normalize_addresses,
    prepare_st_attempt,
    review_st_delivery,
)
from rendering.contexts import PdfRenderNapaka

from .st_invoices import _issued

_SALT = "st-delivery-review-v1"
_PREPARE_ERROR = (
    "Priprava ni dovoljena. Preverite stanje računa, prejemnike, nastavitve pošiljatelja "
    "in celovitost uradnega PDF. Ob spremembi podatkov ponovite pregled."
)
_SEND_ERROR = (
    "Pošiljanje je blokirano ali rezultat ni shranjen. Preverite zadnji poskus, "
    "celovitost uradnega PDF in ujemanje pošiljatelja z nastavitvami SMTP. "
    "Pri začetem pošiljanju uporabite preverjanje prekinjene oddaje."
)
_RESULTS = {
    "PREPARED": "Pripravljeno — še ni poslano.",
    "SUBMISSION_STARTED": "Oddaja se je začela. Ponovno pošiljanje je blokirano.",
    "ACCEPTED": "Odhodni strežnik je sporočilo sprejel. Račun je poslan.",
    "CONFIRMED_FAILURE": "Neuspeh je potrjen. Dovoljena je priprava varnega ponovnega poskusa.",
    "UNKNOWN": "Rezultat ni znan. Preverite mapo Poslano in izrecno zabeležite ugotovitev.",
}
_FAILURES = {
    "SMTP_AUTH_FAILED": "Prijava v poštni predal ni uspela.",
    "SMTP_CONNECT_FAILED": "Poštni strežnik ni dosegljiv.",
    "SMTP_TIMEOUT": "Čas čakanja na poštni strežnik je potekel.",
    "SMTP_CONNECTION_LOST": "Povezava se je prekinila pred oddajo sporočila.",
    "SMTP_SENDER_REJECTED": "Strežnik je zavrnil pošiljatelja.",
    "SMTP_RECIPIENT_REJECTED": "Strežnik je zavrnil prejemnika.",
    "SMTP_DATA_REJECTED": "Strežnik je zavrnil sporočilo.",
    "SMTP_PROTOCOL_ERROR": "Napaka protokola SMTP pred oddajo sporočila.",
    "KEYCHAIN_ERROR": "Dostop do poverilnic ni uspel.",
    "KEYCHAIN_CREDENTIAL_MISSING": "Poverilnice za pošiljanje niso nastavljene.",
}


class STRecipientsForm(forms.Form):
    recipients_to = forms.CharField(label="TO — prejemniki", strip=False)
    recipients_cc = forms.CharField(label="CC — vidna kopija", required=False, strip=False)

    def _addresses(self, name):
        text = self.cleaned_data[name]
        if not text.strip() and name == "recipients_cc":
            return []
        return normalize_addresses(text.replace(";", ",").split(","))

    def clean_recipients_to(self):
        return self._addresses("recipients_to")

    def clean_recipients_cc(self):
        return self._addresses("recipients_cc")


class STDeliveryConfirmForm(forms.Form):
    review_token = forms.CharField(widget=forms.HiddenInput)
    confirm_recipients = forms.BooleanField(label="Potrjujem prikazane prejemnike in pripravo nespremenljivega poskusa.")


def _context(invoice):
    attempt = invoice.delivery_attempts.order_by("-sequence").first()
    result_label = _RESULTS.get(attempt.result, "") if attempt else "Dostava še ni pripravljena."
    if attempt and attempt.result == DeliveryAttempt.Result.UNKNOWN and has_sent_delivery_evidence(invoice, attempt):
        result_label = "Pošiljanje je uporabnik izrecno potrdil s preverjanjem. Izvirni rezultat SMTP ostaja neznan."
    return {
        "invoice": invoice, "attempt": attempt,
        "result_label": result_label,
        "failure_label": _FAILURES.get(attempt.result_code, "Strežnik je potrdil neuspeh oddaje.") if attempt else "",
        "active_section": "dashboard", "active_page": "st",
    }


@transaction.non_atomic_requests
@require_http_methods(["GET", "POST"])
@override("sl")
def st_delivery(request, company_id, invoice_id):
    invoice = _issued(company_id, invoice_id)
    context = _context(invoice)
    suggestion = invoice.prejemnik.email if invoice.prejemnik_id else ""
    form = STRecipientsForm(
        request.POST if request.method == "POST" else None,
        initial={"recipients_to": suggestion},
    )
    if request.method == "POST" and form.is_valid():
        try:
            review = review_st_delivery(invoice, **form.cleaned_data)
        except (ValidationError, PdfRenderNapaka, OSError, DatabaseError):
            context["action_error"] = _PREPARE_ERROR
        else:
            context.update(review=review, confirm_form=STDeliveryConfirmForm(initial={
                "review_token": signing.dumps(review, salt=_SALT),
            }))
    context.update(form=form, suggestion=bool(suggestion))
    return render(request, "ui/st_delivery.html", context)


@transaction.non_atomic_requests
@require_POST
@override("sl")
def st_prepare_delivery(request, company_id, invoice_id):
    invoice = _issued(company_id, invoice_id)
    form = STDeliveryConfirmForm(request.POST)
    if form.is_valid():
        try:
            review = signing.loads(form.cleaned_data["review_token"], salt=_SALT)
            prepare_st_attempt(invoice, review=review)
        except (signing.BadSignature, ValidationError, PdfRenderNapaka, OSError, DatabaseError):
            pass
        else:
            return redirect("ui:st_delivery", company_id=company_id, invoice_id=invoice_id)
    context = _context(invoice)
    context.update(form=STRecipientsForm(), action_error=_PREPARE_ERROR)
    return render(request, "ui/st_delivery.html", context, status=409)


@transaction.non_atomic_requests
@require_POST
@override("sl")
def st_delivery_action(request, company_id, invoice_id, attempt_id):
    invoice = _issued(company_id, invoice_id)
    attempt = get_object_or_404(DeliveryAttempt, pk=attempt_id, invoice_id=invoice.pk)
    action = request.POST.get("action")
    error = _SEND_ERROR if action == "send" else (
        "Dejanje ni dovoljeno. Preverite zadnji poskus in stanje računa. "
        "Za odločitev o neznanem rezultatu vnesite razlog preverjanja."
    )
    try:
        latest = invoice.delivery_attempts.order_by("-sequence").first()
        if latest.pk != attempt.pk:
            raise ValidationError("Zastarel poskus dostave.")
        if action == "send":
            send_pdf_email(
                attempt, subject=f"Račun {invoice.stevilka}",
                body=f"V prilogi je račun {invoice.stevilka}.\n",
                smtp_config=resolve_smtp_config(invoice.company),
                credential_ref=resolve_keyring_credential_ref(invoice.company),
            )
        elif action == "retry":
            prepare_retry_attempt(attempt)
        elif action == "unknown_sent":
            resolve_unknown_as_sent(attempt, reason=request.POST.get("reason", ""))
        elif action == "unknown_not_sent":
            resolve_unknown_user_confirmed_not_sent_and_prepare_retry(
                attempt, reason=request.POST.get("reason", ""),
            )
        elif action == "recover" and attempt.result == DeliveryAttempt.Result.SUBMISSION_STARTED:
            recover_interrupted_submissions(invoice_ids=[invoice.pk])
        else:
            raise ValidationError("Neznano dejanje.")
    except Exception:  # noqa: BLE001 -- operational failures must not expose secrets or resend.
        invoice.refresh_from_db()
        context = _context(invoice)
        context.update(form=STRecipientsForm(), action_error=error)
        return render(request, "ui/st_delivery.html", context, status=409)
    return redirect("ui:st_delivery", company_id=company_id, invoice_id=invoice_id)
