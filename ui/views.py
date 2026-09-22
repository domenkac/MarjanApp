from calendar import monthrange
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_http_methods

from companies.models import Company
from contracts.models import Lease, Tenant
from contracts.services import dnevi_v_obdobju, efektivna_veljavnost_pogodbe
from invoicing.models import Invoice, MonthlyBatch, MonthlyCycle
from invoicing.services import (
    MesecniCikelNapaka,
    aktivni_izracuni_mesecnega_cikla,
    izracunaj_mesecne_kandidate,
    nalozi_aktivni_mesecni_cikel,
    nastavi_izbiro_kandidata,
    potrdi_izjemo_kandidata,
    premakni_mesecni_cikel,
    preveri_predhodno_kontrolo,
    shrani_prvi_korak,
    so_vsi_uspesni_izracuni_aktivni,
    trenutni_pregledi_mesecnega_cikla,
    uskladi_preglede_mesecnega_cikla,
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
    review_error = None
    review_error_identity = None
    form_is_valid = form.is_valid() if request.method == "POST" and not action else False

    review_actions = {"select_candidate", "deselect_candidate", "confirm_candidate"}
    if request.method == "POST" and action in review_actions:
        try:
            if not cycle or cycle.trenutni_korak != MonthlyCycle.Korak.PAKETNI_PREGLED:
                raise ValidationError("Pregled kandidatov je dovoljen samo v koraku 4.")
            lease_id = int(request.POST.get("lease_id", ""))
            fingerprint = request.POST.get("candidate_fingerprint", "")
            review_error_identity = (lease_id, fingerprint)
            if action == "confirm_candidate":
                potrdi_izjemo_kandidata(
                    cycle, lease_id=lease_id, candidate_fingerprint=fingerprint
                )
            else:
                nastavi_izbiro_kandidata(
                    cycle,
                    lease_id=lease_id,
                    candidate_fingerprint=fingerprint,
                    selected=action == "select_candidate",
                    non_issue_reason=request.POST.get("non_issue_reason", ""),
                )
        except (TypeError, ValueError, ValidationError) as exc:
            review_error = _review_error_message(exc)
        else:
            return redirect("ui:mesecni_obracun")

    if request.method == "POST" and action == "rerun_preflight" and cycle:
        if (
            MonthlyCycle.Korak.PREDHODNA_KONTROLA
            <= cycle.trenutni_korak
            <= MonthlyCycle.Korak.PAKETNI_PREGLED
        ):
            preveri_predhodno_kontrolo(cycle)
        return redirect("ui:mesecni_obracun")
    if request.method == "POST" and action == "rerun_calculation" and cycle:
        if cycle.trenutni_korak == MonthlyCycle.Korak.IZRACUN_OSNUTKOV:
            izracunaj_mesecne_kandidate(cycle)
        return redirect("ui:mesecni_obracun")
    if request.method == "POST" and action == "refresh_review" and cycle:
        if cycle.trenutni_korak == MonthlyCycle.Korak.PAKETNI_PREGLED:
            preveri_predhodno_kontrolo(cycle)
            izracunaj_mesecne_kandidate(
                cycle, samo_brez_aktivnega_izracuna=True
            )
            uskladi_preglede_mesecnega_cikla(cycle)
        return redirect("ui:mesecni_obracun")
    if request.method == "POST" and action == "continue_step3" and cycle:
        if (
            cycle.trenutni_korak == MonthlyCycle.Korak.PREDHODNA_KONTROLA
            and cycle.batches.filter(
                preflight_status=MonthlyBatch.PreflightStatus.PASSED
            ).exists()
        ):
            cycle = premakni_mesecni_cikel(
                cycle, MonthlyCycle.Korak.IZRACUN_OSNUTKOV
            )
            izracunaj_mesecne_kandidate(cycle)
        return redirect("ui:mesecni_obracun")
    if request.method == "POST" and action == "continue_step4" and cycle:
        if so_vsi_uspesni_izracuni_aktivni(cycle):
            cycle = premakni_mesecni_cikel(
                cycle, MonthlyCycle.Korak.PAKETNI_PREGLED
            )
            uskladi_preglede_mesecnega_cikla(cycle)
        return redirect("ui:mesecni_obracun")

    if request.method == "POST" and not action:
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
        active_calculation_ids = set(
            aktivni_izracuni_mesecnega_cikla(cycle).values_list("pk", flat=True)
        )
        for batch in batches:
            for issue in batch.preflight_issues:
                issue["location"] = _preflight_location(issue, batch.company, cycle)
            snapshot = batch.calculation_snapshot or {}
            batch.is_active_calculation = batch.pk in active_calculation_ids
            batch.needs_recalculation = (
                batch.preflight_status == MonthlyBatch.PreflightStatus.PASSED
                and batch.calculation_status
                == MonthlyBatch.CalculationStatus.CALCULATED
                and not batch.is_active_calculation
            )
            candidates = (
                snapshot.get("candidates", []) if batch.is_active_calculation else []
            )
            batch.calculation_summary = {
                "candidates": len(candidates),
                "issued": sum(item.get("will_issue", False) for item in candidates),
                "total": sum(
                    Decimal(item.get("totals", {}).get("total", "0.00"))
                    for item in candidates if item.get("will_issue", False)
                ),
                "issues": len(snapshot.get("preflight_warnings", []))
                + sum(len(item.get("exceptions", [])) for item in candidates),
                "blockers": snapshot.get("blockers", []),
            }
        passed = sum(batch.preflight_status == MonthlyBatch.PreflightStatus.PASSED for batch in batches)
        blocked = sum(batch.preflight_status == MonthlyBatch.PreflightStatus.BLOCKED for batch in batches)
        summary = {
            "total": len(batches),
            "passed": passed,
            "blocked": blocked,
            "warnings": sum(issue["severity"] == "WARNING" for batch in batches for issue in batch.preflight_issues),
            "not_checked": sum(batch.preflight_status == MonthlyBatch.PreflightStatus.NOT_CHECKED for batch in batches),
        }

    step3_complete = bool(cycle and so_vsi_uspesni_izracuni_aktivni(cycle))
    review_rows = []
    review_summary = None
    if cycle and cycle.trenutni_korak == MonthlyCycle.Korak.PAKETNI_PREGLED:
        review_rows = _review_rows(cycle, review_error_identity, review_error)
        review_summary = {
            "total": len(review_rows),
            "selected": sum(row["selected"] for row in review_rows),
            "requiring_confirmation": sum(
                row["selected"]
                and row["requires_confirmation"]
                and not row["confirmed_at"]
                for row in review_rows
            ),
            "confirmed": sum(
                row["requires_confirmation"] and bool(row["confirmed_at"])
                for row in review_rows
            ),
            "user_deselected": sum(
                row["will_issue"] and not row["selected"] for row in review_rows
            ),
            "system_non_issuable": sum(
                not row["will_issue"] for row in review_rows
            ),
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
            "step3_complete": step3_complete,
            "step3_can_continue": step3_complete,
            "review_rows": review_rows,
            "review_summary": review_summary,
            "review_error": review_error,
        },
    )


def _review_rows(cycle, error_identity=None, error=None):
    reviews = {
        (review.batch_id, review.lease_id, review.candidate_fingerprint): review
        for review in trenutni_pregledi_mesecnega_cikla(cycle)
    }
    rows = []
    for batch in aktivni_izracuni_mesecnega_cikla(cycle).order_by("company_id", "pk"):
        snapshot = batch.calculation_snapshot
        company_name = snapshot.get("company", {}).get("name", "")
        for candidate in snapshot.get("candidates", []):
            identity = (
                batch.pk,
                candidate["lease_id"],
                candidate["candidate_fingerprint"],
            )
            review = reviews.get(identity)
            if review is None:
                continue
            previous_invoice = candidate.get("previous_invoice")
            current_total = Decimal(candidate["totals"]["total"])
            previous_total = (
                Decimal(previous_invoice["total"]) if previous_invoice else None
            )
            delta_eur = (
                current_total - previous_total if previous_total is not None else None
            )
            delta_percent = (
                delta_eur / previous_total * Decimal(100)
                if previous_total not in (None, Decimal(0))
                else None
            )
            lease_id = candidate["lease_id"]
            fingerprint = candidate["candidate_fingerprint"]
            rows.append(
                {
                    "company_name": company_name,
                    "tenant_name": candidate.get("tenant", {}).get("name", ""),
                    "property_unit_label": " / ".join(
                        filter(
                            None,
                            (candidate.get("property_label"), candidate.get("unit_label")),
                        )
                    ),
                    "contract_reference": candidate.get("contract_reference", ""),
                    "previous_total": previous_total,
                    "current_total": current_total,
                    "delta_eur": delta_eur,
                    "delta_percent": delta_percent,
                    "previous_display": (
                        _money(previous_total) if previous_total is not None else "Nova"
                    ),
                    "current_display": _money(current_total),
                    "delta_eur_display": _signed_money(delta_eur),
                    "delta_percent_display": _signed_percent(delta_percent),
                    "first_issue": bool(candidate.get("first_issue")),
                    "will_issue": bool(candidate["will_issue"]),
                    "system_non_issue_reason": candidate.get("non_issue_reason", ""),
                    "exceptions": candidate.get("exceptions", []),
                    "selected": review.selected,
                    "requires_confirmation": review.requires_confirmation,
                    "confirmed_at": review.confirmed_at,
                    "user_non_issue_reason": review.non_issue_reason,
                    "candidate_fingerprint": fingerprint,
                    "lease_id": lease_id,
                    "error": (
                        error if error_identity == (lease_id, fingerprint) else None
                    ),
                }
            )
    return rows


def _money(value):
    return f"{value:.2f}".replace(".", ",") + " €"


def _signed_money(value):
    if value is None:
        return "—"
    return f"{value:+.2f}".replace(".", ",") + " €" if value else "0,00 €"


def _signed_percent(value):
    if value is None:
        return "—"
    return f"{value:+.2f}".replace(".", ",") + " %" if value else "0,00 %"


def _review_error_message(exc):
    message = exc.messages[0] if isinstance(exc, ValidationError) else "Neveljaven kandidat."
    return {
        "Kandidat ni trenutni aktivni kandidat.": (
            "Kandidat ni več trenutni kandidat. Osvežite paketni pregled."
        ),
        "Neizbranega kandidata ni dovoljeno potrditi.": (
            "Neizbranega kandidata ni mogoče potrditi."
        ),
    }.get(message, message)


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
