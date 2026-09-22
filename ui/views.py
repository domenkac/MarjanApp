from django.shortcuts import render
from django.views.decorators.http import require_GET, require_http_methods

from companies.models import Company
from contracts.models import Lease, Tenant
from invoicing.models import Invoice

from .forms import IzbiraMesecaForm, obdobje_za_mesec


@require_GET
def dashboard(request):
    counts = {
        "podjetja": Company.objects.filter(aktivno=True).count(),
        "najemniki": Tenant.objects.filter(aktiven=True).count(),
        "pogodbe": Lease.objects.filter(
            status=Lease.Status.AKTIVNA,
            arhivirana=False,
        ).count(),
        "izdani_racuni": Invoice.objects.filter(stevilka__isnull=False)
        .exclude(stevilka="")
        .count(),
    }
    return render(
        request,
        "ui/dashboard.html",
        {
            "counts": counts,
            "active_page": "dashboard",
            "active_section": "dashboard",
        },
    )


@require_http_methods(["GET", "POST"])
def mesecni_obracun(request):
    podjetja = Company.objects.filter(aktivno=True).order_by("naziv")
    form = IzbiraMesecaForm(request.POST or None)
    nadaljevanje = None
    form_is_valid = form.is_valid() if request.method == "POST" else False

    if request.method == "POST":
        obdobje_od = form.cleaned_data.get("obdobje_od")
        obdobje_do = form.cleaned_data.get("obdobje_do")
    else:
        obdobje_od, obdobje_do = obdobje_za_mesec(
            form.initial["leto"],
            form.initial["mesec"],
        )

    if form_is_valid and podjetja.exists():
        nadaljevanje = {
            "leto": form.cleaned_data["leto"],
            "mesec": form.cleaned_data["mesec"],
            "datum_izdaje": form.cleaned_data["datum_izdaje"],
            "obdobje_od": form.cleaned_data["obdobje_od"],
            "obdobje_do": form.cleaned_data["obdobje_do"],
        }
        obdobje_od = form.cleaned_data["obdobje_od"]
        obdobje_do = form.cleaned_data["obdobje_do"]

    return render(
        request,
        "ui/mesecni_obracun.html",
        {
            "form": form,
            "podjetja": podjetja,
            "nadaljevanje": nadaljevanje,
            "obdobje_od": obdobje_od,
            "obdobje_do": obdobje_do,
            "active_page": "mesecni_obracun",
            "active_section": "dashboard",
        },
    )
