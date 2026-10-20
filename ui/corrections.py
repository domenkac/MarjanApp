from urllib.parse import urlencode

from django.core import signing
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import OperationalError, transaction
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import override
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from catalogs.models import VatRegime
from companies.models import Company
from invoicing.correction_review import (
    correction_fingerprint,
    correction_review_snapshot,
)
from invoicing.corrections import credit_source, line_fingerprint
from invoicing.models import Invoice
from invoicing.services import (
    dodaj_vrstico,
    izbrisi_vrstico,
    potrdi_korekcijski_osnutek,
    pripravi_korekcijski_osnutek,
    spremeni_podatke_racuna,
    spremeni_vrstico,
)
from rendering.services import render_pdf

from .correction_forms import (
    CorrectionConfirmForm,
    CorrectionDataForm,
    CorrectionDraftForm,
    CorrectionLineForm,
    CorrectionReviewForm,
    source_invoices,
)
from .st_invoices import _PDF_ERROR, _error_response, _pdf_state

_SALT = "ui.correction.review.v1"
_STALE = "Pregled manjka ali je zastarel. Ponovno preglejte trenutne podatke osnutka."
_BUSY = "Baza je zasedena. Preverite stanje dokumenta pred ponovnim poskusom."
_TYPES = [Invoice.DocumentType.STORNO, Invoice.DocumentType.DOBROPIS]


def _correction(company_id, invoice_id, *, draft=False):
    query = Invoice.objects.filter(
        company_id=company_id, pk=invoice_id, document_type__in=_TYPES,
        povezan_racun__company_id=company_id,
        povezan_racun__document_type__in=["NP", "ST"],
        povezan_racun__povezan_racun__isnull=True,
    )
    if draft:
        query = query.filter(status="OSNUTEK", stevilka__isnull=True)
    return get_object_or_404(query)


def _issued(company_id, invoice_id):
    invoice = _correction(company_id, invoice_id)
    if not invoice.stevilka:
        raise Http404
    return invoice


def _context(**values):
    return {"active_section": "corrections", "active_page": "corrections", **values}


def _review_context(invoice):
    return _context(**correction_review_snapshot(invoice))


def _line_source(request, invoice, line):
    salt = _SALT + ".line_source"
    if line:
        if request.GET.get("source_line") or request.POST.get("source_token") or request.POST.get("source_line"):
            raise ValidationError("Izvora shranjene postavke ni dovoljeno zamenjati.")
        return credit_source(line), ""
    token = request.POST.get("source_token", "") if request.method == "POST" else ""
    if request.method == "POST":
        if request.POST.get("source_line") or (request.GET.get("source_line") and not token):
            raise ValidationError("Ponovno izberite izvirno postavko in preglejte njena dokazila DDV.")
        if not token:
            return None, ""
        proof = signing.loads(token, salt=salt)
        if (not isinstance(proof, dict) or proof.get("invoice") != invoice.pk
                or proof.get("company") != invoice.company_id):
            raise signing.BadSignature
        source = get_object_or_404(invoice.povezan_racun.invoiceline_set, pk=proof.get("source"))
        if (proof.get("fingerprint") != line_fingerprint(source)
                or request.GET.get("source_line", str(source.pk)) != str(source.pk)):
            raise signing.BadSignature
        return source, token
    if not request.GET.get("source_line"):
        return None, ""
    try:
        source_id = int(request.GET["source_line"])
    except (ValueError, TypeError):
        raise Http404 from None
    source = get_object_or_404(invoice.povezan_racun.invoiceline_set, pk=source_id)
    return source, signing.dumps({
        "invoice": invoice.pk, "company": invoice.company_id,
        "source": source.pk, "fingerprint": line_fingerprint(source),
    }, salt=salt)


def _token(invoice, phase):
    return signing.dumps({
        "invoice": invoice.pk, "company": invoice.company_id,
        "fingerprint": correction_fingerprint(invoice), "phase": phase,
    }, salt=_SALT, compress=True)


def _proof(token, invoice, phase):
    proof = signing.loads(token, salt=_SALT)
    if (not isinstance(proof, dict) or proof.get("invoice") != invoice.pk
            or proof.get("company") != invoice.company_id or proof.get("phase") != phase
            or not isinstance(proof.get("fingerprint"), str)):
        raise signing.BadSignature
    return proof


def _matches(token, invoice, phase):
    proof = _proof(token, invoice, phase)
    if proof["fingerprint"] != correction_fingerprint(invoice):
        raise signing.BadSignature
    return proof


@require_GET
@override("sl")
def correction_list(request):
    company = None
    if request.GET.get("company"):
        try:
            company_id = int(request.GET["company"])
        except (ValueError, TypeError):
            raise Http404 from None
        company = get_object_or_404(Company, pk=company_id)
    sources = source_invoices(company.pk) if company else Invoice.objects.none()
    invoices = Invoice.objects.filter(document_type__in=_TYPES)
    if company:
        invoices = invoices.filter(company=company)
    search = request.GET.get("q", "").strip()
    if search:
        sources = sources.filter(Q(stevilka__icontains=search) | Q(prejemnik_naziv__icontains=search))
    return render(request, "ui/correction_list.html", _context(
        companies=Company.objects.order_by("naziv", "pk"), company=company, search=search,
        sources=sources.order_by("-datum_izdaje", "-pk"),
        invoices=invoices.order_by("-datum_izdaje", "-pk"),
    ))


@require_http_methods(["GET", "POST"])
@override("sl")
def correction_new(request, company_id):
    company = get_object_or_404(Company, pk=company_id)
    form = CorrectionDraftForm(
        request.POST if request.method == "POST" else None, company_id=company_id,
        initial={"original": request.GET.get("original")},
    )
    if request.method == "POST" and form.is_valid():
        try:
            invoice = pripravi_korekcijski_osnutek(**form.cleaned_data)
        except (ValidationError, ValueError) as error:
            form.add_error(None, str(error))
        except OperationalError:
            form.add_error(None, _BUSY)
        else:
            return redirect("ui:correction_review", company_id=company_id, invoice_id=invoice.pk)
    return render(request, "ui/correction_form.html", _context(form=form, company=company, heading="Nov storno ali dobropis"))


@require_http_methods(["GET", "POST"])
@override("sl")
def correction_edit(request, company_id, invoice_id):
    invoice = _correction(company_id, invoice_id, draft=True)
    form = CorrectionDataForm(request.POST if request.method == "POST" else None, initial={
        "datum_izdaje": invoice.datum_izdaje, "razlog": invoice.razlog,
    })
    if request.method == "POST" and form.is_valid():
        try:
            spremeni_podatke_racuna(invoice, **form.cleaned_data)
        except (ValidationError, ValueError) as error:
            form.add_error(None, str(error))
        except OperationalError:
            form.add_error(None, _BUSY)
        else:
            return redirect("ui:correction_review", company_id=company_id, invoice_id=invoice_id)
    return render(request, "ui/correction_form.html", _context(form=form, invoice=invoice, heading="Datum in razlog popravka"))


@require_http_methods(["GET", "POST"])
@override("sl")
def correction_line(request, company_id, invoice_id, line_id=None):
    invoice = _correction(company_id, invoice_id, draft=True)
    if invoice.document_type != "DOBROPIS":
        raise Http404
    line = get_object_or_404(invoice.invoiceline_set, pk=line_id) if line_id else None
    try:
        source, source_token = _line_source(request, invoice, line)
    except (signing.BadSignature, ValidationError, ValueError):
        return _error_response("Izvor postavke ni veljaven. Ponovno izberite izvirno postavko.")
    initial = CorrectionLineForm.initial_from_line(line or source, source=line is None) if line or source else {}
    initial["source_token"] = source_token
    form = CorrectionLineForm(request.POST if request.method == "POST" else None,
                              initial=initial, historical_source=source)
    if request.method == "POST" and form.is_valid():
        try:
            data = {name: value for name, value in form.cleaned_data.items() if name != "source_token"}
            if line:
                spremeni_vrstico(line, **data)
            else:
                dodaj_vrstico(invoice, izvirna_vrstica=source, **data)
        except (ValidationError, ValueError) as error:
            form.add_service_error(error)
        except OperationalError:
            form.add_error(None, _BUSY)
        else:
            return redirect("ui:correction_review", company_id=company_id, invoice_id=invoice_id)
    return render(request, "ui/correction_form.html", _context(
        form=form, invoice=invoice, heading="Postavka dobropisa", source=source,
        source_lines=invoice.povezan_racun.invoiceline_set.order_by("pk") if not line else [],
    ))


@require_POST
@override("sl")
def correction_line_delete(request, company_id, invoice_id, line_id):
    invoice = _correction(company_id, invoice_id, draft=True)
    if invoice.document_type != "DOBROPIS":
        raise Http404
    line = get_object_or_404(invoice.invoiceline_set, pk=line_id)
    try:
        izbrisi_vrstico(line)
    except (ValidationError, ValueError):
        return _error_response(_STALE)
    except OperationalError:
        return _error_response(_BUSY)
    return redirect("ui:correction_review", company_id=company_id, invoice_id=invoice_id)


@require_http_methods(["GET", "POST"])
@override("sl")
def correction_review(request, company_id, invoice_id):
    try:
        with transaction.atomic():
            invoice = _correction(company_id, invoice_id, draft=True)
            context = _review_context(invoice)
            form = CorrectionReviewForm(request.POST if request.method == "POST" else None,
                                        initial={"token": _token(invoice, "review")})
            if request.method == "POST" and form.is_valid():
                _matches(form.cleaned_data["token"], invoice, "review")
                token = _token(invoice, "confirm")
                response = redirect("ui:correction_confirm", company_id=company_id, invoice_id=invoice_id)
                response["Location"] += "?" + urlencode({"token": token})
                return response
            context["form"] = form
    except (signing.BadSignature, ValueError, ObjectDoesNotExist):
        return _error_response(_STALE)
    except OperationalError:
        return _error_response(_BUSY)
    return render(request, "ui/correction_review.html", context)


@transaction.non_atomic_requests
@require_http_methods(["GET", "POST"])
@override("sl")
def correction_confirm(request, company_id, invoice_id):
    invoice = _correction(company_id, invoice_id)
    token = (request.POST if request.method == "POST" else request.GET).get("token", "")
    form = CorrectionConfirmForm(request.POST if request.method == "POST" else None, initial={"token": token})
    try:
        proof = _proof(token, invoice, "confirm")
        if request.method == "POST":
            if not form.is_valid():
                return _error_response("Izrecno potrdite dokončno izdajo.", status=400)
            if invoice.stevilka:
                return redirect("ui:correction_detail", company_id=company_id, invoice_id=invoice_id)
            issued = potrdi_korekcijski_osnutek(invoice, expected_fingerprint=proof["fingerprint"])
            return _render_and_redirect(issued)
        with transaction.atomic():
            invoice = _correction(company_id, invoice_id, draft=True)
            _matches(token, invoice, "confirm")
            context = _review_context(invoice)
    except (signing.BadSignature, ValidationError, ValueError, ObjectDoesNotExist) as error:
        message = _STALE if isinstance(error, signing.BadSignature) else f"{_STALE} {error}"
        return _error_response(message)
    except OSError:
        return _error_response("Izdaja ni uspela. Preverite dostopnost slik izdajatelja in ponovno preglejte osnutek.")
    except OperationalError:
        return _error_response(_BUSY)
    context.update(form=form, confirming=True)
    return render(request, "ui/correction_review.html", context)


def _render_and_redirect(invoice):
    failed = False
    try:
        render_pdf(invoice)
    except Exception:  # noqa: BLE001 -- issuance is already committed; never repeat it.
        failed = True
    response = redirect("ui:correction_detail", company_id=invoice.company_id, invoice_id=invoice.pk)
    if failed:
        response["Location"] += "?pdf_error=1"
    return response


@transaction.non_atomic_requests
@require_GET
@override("sl")
def correction_detail(request, company_id, invoice_id):
    from .correction_delivery import delivery_context

    invoice = _issued(company_id, invoice_id)
    context = _review_context(invoice)
    context.update(delivery_context(invoice))
    context.update(pdf_state=_pdf_state(invoice), pdf_error=_PDF_ERROR if request.GET.get("pdf_error") else None)
    return render(request, "ui/correction_detail.html", context)


@transaction.non_atomic_requests
@require_POST
@override("sl")
def correction_retry_pdf(request, company_id, invoice_id):
    return _render_and_redirect(_issued(company_id, invoice_id))


@require_GET
@override("sl")
def correction_pdf(request, company_id, invoice_id):
    invoice = _issued(company_id, invoice_id)
    state = _pdf_state(invoice)
    if state["state"] != "READY":
        return _error_response(_PDF_ERROR)
    response = HttpResponse(state["result"].pdf_bytes, content_type="application/pdf")
    disposition = "attachment" if request.GET.get("download") == "1" else "inline"
    response["Content-Disposition"] = f'{disposition}; filename="korekcija-{invoice.pk}.pdf"'
    response["Cache-Control"] = "no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@require_GET
@override("sl")
def correction_original(request, company_id, invoice_id):
    invoice = get_object_or_404(source_invoices(company_id), pk=invoice_id)
    lines = list(invoice.invoiceline_set.order_by("pk"))
    labels = dict(VatRegime.NacinObracuna.choices)
    for line in lines:
        line.vat_method_label = labels.get(line.ddv_nacin_obracuna, line.ddv_nacin_obracuna)
    return render(request, "ui/correction_original.html", _context(invoice=invoice, lines=lines))
