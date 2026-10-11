from decimal import Decimal

from django import forms
from django.core.exceptions import ValidationError
from django.db import OperationalError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from companies.models import Company
from contracts.indexation import (
    batch_is_stale,
    confirm_batch,
    create_batch,
    save_review,
)
from contracts.models import IndexationBatch


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
            "active_section": "pogodbe",
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
            "active_section": "pogodbe",
        },
    )
