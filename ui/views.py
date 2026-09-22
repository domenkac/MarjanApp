from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_http_methods

from companies.models import Company
from contracts.models import Lease, Tenant
from invoicing.models import Invoice, MonthlyCycle
from invoicing.services import (
    MesecniCikelNapaka,
    nalozi_aktivni_mesecni_cikel,
    shrani_prvi_korak,
)

from .forms import IzbiraMesecaForm, obdobje_za_mesec


@require_GET
def dashboard(request):
    aktivni_cikel = nalozi_aktivni_mesecni_cikel()
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
            "aktivni_cikel": aktivni_cikel,
        },
    )


@require_http_methods(["GET", "POST"])
def mesecni_obracun(request):
    cycle = nalozi_aktivni_mesecni_cikel()
    podjetja = (
        Company.objects.filter(monthlybatch__cycle=cycle).order_by("naziv")
        if cycle
        else Company.objects.filter(aktivno=True).order_by("naziv")
    )
    initial = None
    if cycle:
        initial = {
            "leto": cycle.obdobje.year,
            "mesec": cycle.obdobje.month,
            "datum_izdaje": cycle.datum_izdaje,
        }
    form = (
        IzbiraMesecaForm(request.POST or None, initial=initial)
        if initial
        else IzbiraMesecaForm(request.POST or None)
    )
    nadaljevanje = None
    sporocilo_napake = None
    form_is_valid = form.is_valid() if request.method == "POST" else False

    if request.method == "POST":
        obdobje_od = form.cleaned_data.get("obdobje_od")
        obdobje_do = form.cleaned_data.get("obdobje_do")
    else:
        obdobje_od, obdobje_do = obdobje_za_mesec(
            form.initial["leto"],
            form.initial["mesec"],
        )

    if form_is_valid:
        try:
            shrani_prvi_korak(
                form.cleaned_data["obdobje_od"],
                form.cleaned_data["datum_izdaje"],
            )
        except MesecniCikelNapaka as exc:
            sporocilo_napake = str(exc)
        else:
            return redirect("ui:mesecni_obracun")

    if cycle and cycle.trenutni_korak == MonthlyCycle.Korak.PREDHODNA_KONTROLA:
        nadaljevanje = {
            "leto": cycle.obdobje.year,
            "mesec": cycle.obdobje.month,
            "datum_izdaje": cycle.datum_izdaje,
        }
        obdobje_od, obdobje_do = obdobje_za_mesec(
            cycle.obdobje.year, cycle.obdobje.month
        )

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
            "trenutni_korak": cycle.trenutni_korak if cycle else 1,
            "sporocilo_napake": sporocilo_napake,
        },
    )
