from calendar import monthrange

from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_http_methods

from companies.models import Company
from contracts.models import Lease, Tenant
from contracts.services import dnevi_v_obdobju, efektivna_veljavnost_pogodbe
from invoicing.models import Invoice, MonthlyBatch, MonthlyCycle
from invoicing.services import (
    MesecniCikelNapaka,
    nalozi_aktivni_mesecni_cikel,
    premakni_mesecni_cikel,
    preveri_predhodno_kontrolo,
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
    action = request.POST.get("action") if request.method == "POST" else None
    form = (
        IzbiraMesecaForm(request.POST or None, initial=initial)
        if initial
        else IzbiraMesecaForm(request.POST or None)
    )
    nadaljevanje = None
    sporocilo_napake = None
    form_is_valid = form.is_valid() if request.method == "POST" and not action else False

    if (
        request.method == "POST"
        and action == "rerun_preflight"
        and cycle
        and cycle.trenutni_korak >= MonthlyCycle.Korak.PREDHODNA_KONTROLA
    ):
        preveri_predhodno_kontrolo(cycle)
        return redirect("ui:mesecni_obracun")
    if request.method == "POST" and action == "continue_step3" and cycle:
        if (
            cycle.trenutni_korak == MonthlyCycle.Korak.PREDHODNA_KONTROLA
            and cycle.batches.filter(
                preflight_status=MonthlyBatch.PreflightStatus.PASSED
            ).exists()
        ):
            premakni_mesecni_cikel(cycle, MonthlyCycle.Korak.IZRACUN_OSNUTKOV)
        return redirect("ui:mesecni_obracun")

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
            cycle = shrani_prvi_korak(
                form.cleaned_data["obdobje_od"],
                form.cleaned_data["datum_izdaje"],
            )
        except MesecniCikelNapaka as exc:
            sporocilo_napake = str(exc)
        else:
            preveri_predhodno_kontrolo(cycle)
            return redirect("ui:mesecni_obracun")

    batches = []
    summary = None
    if cycle and cycle.trenutni_korak >= MonthlyCycle.Korak.PREDHODNA_KONTROLA:
        nadaljevanje = {
            "leto": cycle.obdobje.year,
            "mesec": cycle.obdobje.month,
            "datum_izdaje": cycle.datum_izdaje,
        }
        obdobje_od, obdobje_do = obdobje_za_mesec(
            cycle.obdobje.year, cycle.obdobje.month
        )
        batches = list(cycle.batches.select_related("company").order_by("company__naziv"))
        for batch in batches:
            for issue in batch.preflight_issues:
                issue["location"] = _preflight_location(issue, batch.company, cycle)
        passed = sum(batch.preflight_status == MonthlyBatch.PreflightStatus.PASSED for batch in batches)
        blocked = sum(batch.preflight_status == MonthlyBatch.PreflightStatus.BLOCKED for batch in batches)
        summary = {
            "total": len(batches),
            "passed": passed,
            "blocked": blocked,
            "warnings": sum(issue["severity"] == "WARNING" for batch in batches for issue in batch.preflight_issues),
            "not_checked": sum(batch.preflight_status == MonthlyBatch.PreflightStatus.NOT_CHECKED for batch in batches),
        }

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
            "batches": batches,
            "preflight_summary": summary,
            "step2_all_passed": bool(summary and summary["passed"] == summary["total"]),
            "step2_partial": bool(summary and summary["passed"] and summary["blocked"]),
        },
    )


def _preflight_location(issue, company, cycle):
    code = issue["code"]
    if code.startswith("DDV_POTRDITEV_"):
        return "Nastavitve → DDV"
    if code.startswith("VZOREC_"):
        return "Nastavitve → Vzorec računa"
    if code.startswith("PODPISNIK_") or issue["model"] == "Company":
        return "Nastavitve → Podjetje"
    model = issue["model"]
    leases = Lease.objects.filter(
        unit__property__company=company,
        arhivirana=False,
        unit__property__aktivna=True,
        unit__aktivna=True,
        tenant__aktiven=True,
    )
    if model == "Lease":
        leases = leases.filter(pk=issue["object_id"])
    elif model == "LeaseItem":
        leases = leases.filter(leaseitem__pk=issue["object_id"])
    elif model == "Tenant":
        leases = leases.filter(tenant_id=issue["object_id"])
    elif model == "Unit":
        leases = leases.filter(unit_id=issue["object_id"])
    elif model == "Property":
        leases = leases.filter(unit__property_id=issue["object_id"])
    elif model == "VatRegime":
        leases = leases.filter(leaseitem__vat_regime_id=issue["object_id"])
    else:
        return company.naziv
    period_end = cycle.obdobje.replace(
        day=monthrange(cycle.obdobje.year, cycle.obdobje.month)[1]
    )
    labels = []
    for lease in leases.distinct().order_by("oznaka_pogodbe", "pk"):
        start, end = efektivna_veljavnost_pogodbe(lease)
        if dnevi_v_obdobju(cycle.obdobje, period_end, start, end):
            labels.append(lease.oznaka_pogodbe)
    if labels:
        return f"Pogodbe → {', '.join(labels)}"
    return company.naziv
