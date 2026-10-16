from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import OperationalError
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import override
from django.views.decorators.http import require_GET, require_http_methods

from catalogs.models import VatRegime
from invoicing.models import Invoice
from invoicing.services import pripravi_osnutek_st

from .st_forms import STDraftForm, STListFilterForm


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
    invoice = get_object_or_404(
        Invoice.objects.filter(Q(stevilka__isnull=True) | Q(stevilka="")),
        pk=invoice_id, company_id=company_id, document_type=Invoice.DocumentType.ST,
        status=Invoice.Status.OSNUTEK, lease__isnull=True,
    )
    lines = list(invoice.invoiceline_set.order_by("pk"))
    vat_labels = dict(VatRegime.NacinObracuna.choices)
    for line in lines:
        line.vat_method_label = vat_labels.get(line.ddv_nacin_obracuna, line.ddv_nacin_obracuna)
    return render(request, "ui/st_review.html", {
        "invoice": invoice, "lines": lines,
        "active_section": "dashboard", "active_page": "st",
    })
