import json

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import OperationalError, transaction
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import override
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from catalogs.models import VatRegime
from core.models import AuditLogEntry
from invoicing.models import Invoice
from invoicing.services import (
    fingerprint_osnutka_st,
    potrdi_osnutek_st,
    preglej_osnutek_st,
    pripravi_osnutek_st,
)
from rendering.contexts import PdfRenderNapaka
from rendering.services import render_pdf, stanje_koncnega_pdf

from .st_forms import STConfirmForm, STDraftForm, STListFilterForm, STReviewForm

_REVIEW_ERROR = (
    "Pregled manjka, je zastarel ali podatki niso veljavni. "
    "Ponovno odprite pregled osnutka in preverite podatke ter nastavitve izdajatelja. "
    "Če se shranjeni posnetki ne ujemajo z izvornimi podatki, pripravite nov osnutek."
)
_PDF_ERROR = (
    "Račun je izdan. PDF ni na voljo ali njegova celovitost ni potrjena. "
    "Ponovite samo pripravo PDF. Registriranega manjkajočega ali poškodovanega PDF "
    "ni dovoljeno samodejno zamenjati; potreben je pregled shranjenega dokumenta."
)


def _draft(company_id, invoice_id):
    return get_object_or_404(
        Invoice.objects.filter(Q(stevilka__isnull=True) | Q(stevilka="")),
        pk=invoice_id, company_id=company_id, document_type=Invoice.DocumentType.ST,
        status=Invoice.Status.OSNUTEK, lease__isnull=True, povezan_racun__isnull=True,
    )


def _issued(company_id, invoice_id):
    return get_object_or_404(
        Invoice.objects.exclude(Q(stevilka__isnull=True) | Q(stevilka="")),
        pk=invoice_id, company_id=company_id, document_type=Invoice.DocumentType.ST,
        lease__isnull=True, povezan_racun__isnull=True,
    )


def _snapshot_context(invoice):
    lines = list(invoice.invoiceline_set.order_by("pk"))
    vat_labels = dict(VatRegime.NacinObracuna.choices)
    for line in lines:
        line.vat_method_label = vat_labels.get(line.ddv_nacin_obracuna, line.ddv_nacin_obracuna)
    return {
        "invoice": invoice, "lines": lines,
        "active_section": "dashboard", "active_page": "st",
    }


def _evidence(invoice, review_id=None):
    entries = AuditLogEntry.objects.filter(
        content_type=ContentType.objects.get_for_model(Invoice), object_id=invoice.pk,
        polje="st_individual_review_v1",
    )
    if review_id is not None:
        entries = entries.filter(pk=review_id)
    return entries.order_by("-pk")


def _draft_context(company_id, invoice_id, *, review_id=None):
    # Read the displayed snapshots and their fingerprint from one database state.
    # Fingerprinting validates database data only; GET never freezes assets.
    with transaction.atomic():
        invoice = _draft(company_id, invoice_id)
        context = _snapshot_context(invoice)
        context.update(fingerprint=None, review=None)
        try:
            fingerprint = fingerprint_osnutka_st(invoice)
        except (ValidationError, ValueError, ObjectDoesNotExist):
            context["action_error"] = _REVIEW_ERROR
        else:
            context["fingerprint"] = fingerprint
            # This match is for display only. Confirmation always revalidates
            # the exact persisted evidence through potrdi_osnutek_st.
            for entry in _evidence(invoice, review_id):
                try:
                    matches = json.loads(entry.nova_vrednost).get("fingerprint") == fingerprint
                except (ValueError, TypeError, AttributeError):
                    matches = False
                if matches:
                    context["review"] = entry
                    break
    return context


def _error_response(message, *, status=409):
    return HttpResponse(message, status=status, content_type="text/plain; charset=utf-8")


@require_GET
@override("sl")
def st_list(request):
    form = STListFilterForm(request.GET)
    invoices = Invoice.objects.filter(document_type=Invoice.DocumentType.ST)
    if not form.is_valid():
        invoices = invoices.none()
    elif form.cleaned_data["company"]:
        invoices = invoices.filter(company=form.cleaned_data["company"])
    return render(request, "ui/st_list.html", {
        "form": form,
        "invoices": invoices.order_by("company_id", "-datum_izdaje", "-pk"),
        "active_section": "dashboard", "active_page": "st",
    })


@require_http_methods(["GET", "POST"])
@override("sl")
def st_new(request):
    form = STDraftForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            invoice = pripravi_osnutek_st(**form.service_arguments())
        except ValidationError as error:
            # Domain errors may name model fields absent from this input form.
            for message in error.messages:
                form.add_error(None, message)
        except ValueError as error:
            form.add_error(None, str(error))
        except ObjectDoesNotExist:
            form.add_error(None, "Izbrani zapis ne obstaja več. Preverite izbiro.")
        except OperationalError:
            form.add_error(None, "Baza je zasedena. Ponovite zahtevo.")
        else:
            return redirect("ui:st_review", company_id=invoice.company_id, invoice_id=invoice.pk)
    return render(request, "ui/st_form.html", {
        "form": form, "active_section": "dashboard", "active_page": "st",
    })


@require_GET
@override("sl")
def st_review(request, company_id, invoice_id):
    try:
        context = _draft_context(company_id, invoice_id)
    except OperationalError:
        return _error_response("Baza je zasedena. Ponovno odprite pregled osnutka.")
    context["form"] = STReviewForm(initial={"fingerprint": context["fingerprint"]})
    return render(request, "ui/st_review.html", context)


@transaction.non_atomic_requests
@require_POST
@override("sl")
def st_record_review(request, company_id, invoice_id):
    invoice = _draft(company_id, invoice_id)
    form = STReviewForm(request.POST)
    if not form.is_valid():
        return _error_response("Izrecno potrdite pregled vseh prikazanih podatkov.", status=400)
    try:
        proof = preglej_osnutek_st(
            invoice, expected_fingerprint=form.cleaned_data["fingerprint"],
            individual_review=form.cleaned_data["individual_review"],
        )
    except (ValidationError, ValueError, ObjectDoesNotExist, OSError):
        return _error_response(_REVIEW_ERROR)
    except OperationalError:
        return _error_response("Baza je zasedena. Ponovno odprite pregled osnutka.")
    response = redirect("ui:st_confirm", company_id=company_id, invoice_id=invoice_id)
    response["Location"] += f"?review_id={proof.pk}"
    return response


@transaction.non_atomic_requests
@require_http_methods(["GET", "POST"])
@override("sl")
def st_confirm(request, company_id, invoice_id):
    invoice = _draft(company_id, invoice_id)
    source = request.POST if request.method == "POST" else request.GET
    try:
        review_id = STConfirmForm.base_fields["review_id"].clean(source.get("review_id"))
    except ValidationError:
        return _error_response(_REVIEW_ERROR)
    form = STConfirmForm(
        request.POST if request.method == "POST" else None,
        initial={"review_id": review_id},
    )
    error = None
    if request.method == "POST" and form.is_valid():
        try:
            proof = _evidence(invoice, review_id).first()
            # The service commits numbering before returning. PDF generation
            # must remain outside both its transaction and ATOMIC_REQUESTS.
            issued = potrdi_osnutek_st(invoice, pregled=proof)
        except (ValidationError, ValueError, ObjectDoesNotExist, OSError):
            error = _REVIEW_ERROR
        except OperationalError:
            return _error_response("Baza je zasedena. Preverite stanje računa pred ponovnim poskusom.")
        else:
            return _render_and_redirect(issued)
    try:
        context = _draft_context(company_id, invoice_id, review_id=review_id)
    except OperationalError:
        return _error_response("Baza je zasedena. Ponovno odprite pregled osnutka.")
    if context["review"] is None:
        error = _REVIEW_ERROR
    context.update(form=form, action_error=error or context.get("action_error"))
    return render(request, "ui/st_confirm.html", context, status=409 if error else 200)


def _render_and_redirect(invoice):
    failed = False
    try:
        render_pdf(invoice)
    except Exception:  # noqa: BLE001 -- PDF failures must never repeat issuance.
        failed = True
    response = redirect("ui:st_detail", company_id=invoice.company_id, invoice_id=invoice.pk)
    if failed:
        response["Location"] += "?pdf_error=1"
    return response


def _pdf_state(invoice):
    try:
        return stanje_koncnega_pdf(invoice)
    except (PdfRenderNapaka, OSError, ValueError):
        return {"state": "ERROR"}


@require_GET
@override("sl")
def st_detail(request, company_id, invoice_id):
    invoice = _issued(company_id, invoice_id)
    context = _snapshot_context(invoice)
    context.update(pdf_state=_pdf_state(invoice), pdf_error=request.GET.get("pdf_error") == "1")
    return render(request, "ui/st_detail.html", context)


@transaction.non_atomic_requests
@require_POST
@override("sl")
def st_retry_pdf(request, company_id, invoice_id):
    return _render_and_redirect(_issued(company_id, invoice_id))


@require_GET
@override("sl")
def st_pdf(request, company_id, invoice_id):
    invoice = _issued(company_id, invoice_id)
    state = _pdf_state(invoice)
    if state["state"] != "READY":
        return _error_response(_PDF_ERROR)
    response = HttpResponse(state["result"].pdf_bytes, content_type="application/pdf")
    disposition = "attachment" if request.GET.get("download") == "1" else "inline"
    response["Content-Disposition"] = f'{disposition}; filename="racun-st-{invoice.pk}.pdf"'
    response["Cache-Control"] = "no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response
