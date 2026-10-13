from decimal import Decimal

from django import forms
from django.core.exceptions import ValidationError
from django.db import OperationalError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from companies.models import Company
from contracts.adjustments import (
    adjustment_approval_blockers,
    adjustment_decision_audit,
    adjustment_review_token,
    decide_adjustment,
    review_adjustments,
    unresolved_warnings,
)
from contracts.indexation import (
    batch_is_stale,
    confirm_batch,
    create_batch,
    save_review,
)
from contracts.models import IndexationBatch, Lease, PoracunIndeksacije


class CompanyChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return obj.naziv


class IndexInputForm(forms.Form):
    company = CompanyChoiceField(
        queryset=Company.objects.filter(aktivno=True), label="Podjetje"
    )
    index_year = forms.IntegerField(min_value=1, max_value=9998, label="Leto indeksa")
    index_value = forms.DecimalField(
        max_digits=6,
        decimal_places=3,
        min_value=Decimal("0.001"),
        label="Objavljeni kumulativni indeks",
    )


@require_http_methods(["GET", "POST"])
def indexation_list(request):
    form = IndexInputForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            batch = create_batch(
                form.cleaned_data["company"].pk,
                form.cleaned_data["index_year"],
                form.cleaned_data["index_value"],
            )
        except (ValidationError, OperationalError) as exc:
            form.add_error(
                None,
                "; ".join(exc.messages)
                if isinstance(exc, ValidationError)
                else "Baza je zasedena. Ponovite zahtevo.",
            )
        else:
            return redirect(
                "ui:indexation_detail", company_id=batch.company_id, batch_id=batch.pk
            )
    return render(
        request,
        "ui/indexation_list.html",
        {
            "form": form,
            "batches": IndexationBatch.objects.select_related("company").order_by(
                "-pk"
            ),
            "adjustment_warnings": [
                warning
                for company_id in Company.objects.order_by("pk").values_list("pk", flat=True)
                for warning in unresolved_warnings(company_id)
            ],
            "active_section": "pogodbe",
            "active_page": "pogodbe",
        },
    )


@require_http_methods(["GET", "POST"])
def indexation_detail(request, company_id, batch_id):
    batch = get_object_or_404(
        IndexationBatch.objects.select_related("company"),
        pk=batch_id,
        company_id=company_id,
    )
    error = ""
    if request.method == "POST":
        try:
            revision = int(request.POST.get("revision", ""))
            action = request.POST.get("action")
            if action == "save":
                selected = {int(value) for value in request.POST.getlist("selected")}
                exclusions = {
                    int(key.removeprefix("exclude_")): request.POST.get(
                        "reason_" + key.removeprefix("exclude_"), ""
                    )
                    for key in request.POST
                    if key.startswith("exclude_")
                }
                save_review(company_id, batch_id, revision, selected, exclusions)
            elif action == "confirm":
                confirm_batch(company_id, batch_id, revision)
            else:
                raise ValidationError("Neveljavna zahteva.")
        except (ValidationError, ValueError, OperationalError) as exc:
            error = (
                "; ".join(exc.messages)
                if isinstance(exc, ValidationError)
                else "Zahteva ni veljavna ali je baza zasedena. Ponovno odprite pregled."
            )
        else:
            return redirect(
                "ui:indexation_detail", company_id=company_id, batch_id=batch_id
            )
        batch.refresh_from_db()
    groups = {}
    for decision in batch.decisions.select_related("lease").order_by(
        "lease_id", "lease_item_id", "pk"
    ):
        groups.setdefault(
            decision.lease_id,
            {
                "lease": decision.lease,
                "reason": decision.exclusion_reason,
                "decisions": [],
            },
        )["decisions"].append(decision)
    return render(
        request,
        "ui/indexation_detail.html",
        {
            "batch": batch,
            "groups": groups.values(),
            "error": error,
            "stale": batch_is_stale(batch),
            "adjustment_warnings": unresolved_warnings(company_id),
            "active_section": "pogodbe",
            "active_page": "pogodbe",
        },
    )


class AdjustmentFilterForm(forms.Form):
    company = CompanyChoiceField(
        queryset=Company.objects.order_by("naziv", "pk"),
        required=False, label="Podjetje",
    )
    lease = forms.ModelChoiceField(
        queryset=Lease.objects.none(), required=False, label="Pogodba",
    )
    evidence_state = forms.ChoiceField(
        choices=[("", "Vsa stanja dokazil"), *PoracunIndeksacije.EvidenceState.choices],
        required=False, label="Stanje dokazil",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        leases = Lease.objects.order_by("oznaka_pogodbe", "pk")
        company = self.data.get("company", "")
        if company:
            try:
                company = self.fields["company"].clean(company)
            except ValidationError:
                leases = leases.none()
            else:
                leases = leases.filter(unit__property__company=company)
        self.fields["lease"].queryset = leases


def _review_display(row):
    calculation = row.calculation_evidence.get("calculation") or {}
    return {
        "row": row,
        "old_amount": calculation.get("billed_original_net"),
        "new_amount": calculation.get("new_period_net"),
        "signed_difference": (
            f"{row.znesek_poracuna:+.2f}"
            if row.znesek_poracuna is not None else None
        ),
        "vat": row.calculation_evidence.get("source_line", {}),
        "blockers": adjustment_approval_blockers(row),
    }


@require_http_methods(["GET"])
def adjustment_list(request):
    form = AdjustmentFilterForm(request.GET)
    groups = {}
    if form.is_valid():
        company = form.cleaned_data["company"]
        rows = review_adjustments(company.pk if company else None)
        if form.cleaned_data["lease"]:
            rows = rows.filter(
                indexation_event__lease_item__lease=form.cleaned_data["lease"]
            )
        if form.cleaned_data["evidence_state"]:
            rows = rows.filter(evidence_state=form.cleaned_data["evidence_state"])
        for row in rows.order_by(
            "original_invoice__company_id", "indexation_event__lease_item__lease_id",
            "evidence_state", "pk",
        ):
            lease = row.indexation_event.lease_item.lease
            key = (row.original_invoice.company_id, lease.pk, row.evidence_state)
            group = groups.setdefault(key, {
                "company": row.original_invoice.company,
                "lease": lease,
                "evidence_state": row.get_evidence_state_display(),
                "entries": [],
            })
            group["entries"].append(_review_display(row))
    return render(request, "ui/adjustment_list.html", {
        "form": form, "groups": list(groups.values()),
        "active_section": "pogodbe", "active_page": "pogodbe",
    })


@require_http_methods(["GET", "POST"])
def adjustment_detail(request, company_id, adjustment_id):
    row = get_object_or_404(review_adjustments(company_id), pk=adjustment_id)
    error = ""
    reason = ""
    if request.method == "POST":
        reason = request.POST.get("reason", "")
        try:
            decide_adjustment(
                company_id, adjustment_id, request.POST.get("token", ""),
                request.POST.get("action"), reason=reason,
            )
        except (ValidationError, OperationalError) as exc:
            error = (
                "; ".join(exc.messages) if isinstance(exc, ValidationError)
                else "Baza je zasedena. Ponovno odprite pregled."
            )
        else:
            return redirect(
                "ui:adjustment_detail", company_id=company_id,
                adjustment_id=adjustment_id,
            )
        row = get_object_or_404(review_adjustments(company_id), pk=adjustment_id)
    return render(request, "ui/adjustment_detail.html", {
        **_review_display(row), "token": adjustment_review_token(row), "error": error,
        "reason": reason, "decision_audit": adjustment_decision_audit(row),
        "adjustment_warnings": unresolved_warnings(company_id),
        "active_section": "pogodbe", "active_page": "pogodbe",
    })
