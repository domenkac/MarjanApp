"""First-run navigation. Business records and existing services own readiness."""

from django import forms
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import override
from django.views.decorators.http import require_http_methods

from archive.configuration import ArchiveConfigurationError, validate_archive_locations
from archive.models import ArchiveConfiguration
from companies.models import Company
from companies.services import veljavna_potrditev_ddv, veljavna_potrditev_vzorca
from companies.setup import issuer_setup_issues
from contracts.models import Lease, Property, Tenant, Unit
from contracts.setup import lease_items_setup_errors, record_setup_errors

from .models import SetupNavigation

STEPS = (
    ("company", "Podjetje"),
    ("property", "Nepremičnina"),
    ("unit", "Enota"),
    ("tenant", "Najemnik"),
    ("lease", "Pogodba"),
    ("items", "Pogodbene postavke"),
    ("issuer", "Nastavitve izdajatelja"),
)


class SetupChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return str(
            getattr(obj, "naziv", None)
            or getattr(obj, "oznaka", None)
            or getattr(obj, "oznaka_pogodbe", None)
            or f"Zapis {obj.pk} (manjka naziv)"
        )


class SetupSelectionForm(forms.Form):
    def __init__(self, *args, queryset, label, selected=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["record"] = SetupChoiceField(
            queryset=queryset,
            label=label,
            initial=selected,
            error_messages={
                "required": "Izberite zapis ali ga najprej dodajte.",
                "invalid_choice": "Izbrani zapis ni aktiven ali ne pripada izbrani verigi.",
            },
        )


def _navigation():
    state, _ = SetupNavigation.objects.get_or_create(pk=1)
    # Re-read and discard selections invalidated by ordinary record maintenance.
    company = Company.objects.filter(pk=state.company_id, aktivno=True).first()
    property_ = (
        Property.objects.filter(
            pk=state.property_id, company=company, aktivna=True
        ).first()
        if company
        else None
    )
    unit = (
        Unit.objects.filter(pk=state.unit_id, property=property_, aktivna=True).first()
        if property_
        else None
    )
    tenant = (
        Tenant.objects.filter(pk=state.tenant_id, aktiven=True).first()
        if company
        else None
    )
    lease = (
        Lease.objects.select_related("unit__property__company", "tenant")
        .filter(
            pk=state.lease_id,
            unit=unit,
            tenant=tenant,
            arhivirana=False,
        )
        .first()
        if unit and tenant
        else None
    )
    changed = []
    for name, record in (
        ("company", company),
        ("property", property_),
        ("unit", unit),
        ("tenant", tenant),
        ("lease", lease),
    ):
        if getattr(state, f"{name}_id") != getattr(record, "pk", None):
            changed.append(name)
        setattr(state, name, record)
    if state.step not in dict(STEPS):
        state.step = "company"
        changed.append("step")
    if changed:
        state.save(update_fields=changed)
    return state


def _choices(state, step):
    if step == "company":
        return Company.objects.filter(aktivno=True).order_by("naziv", "pk")
    if step == "property":
        return Property.objects.filter(
            company_id=state.company_id, aktivna=True
        ).order_by("naziv", "pk")
    if step == "unit":
        return Unit.objects.filter(
            property_id=state.property_id, aktivna=True
        ).order_by("oznaka", "pk")
    if step == "tenant":
        return (
            Tenant.objects.filter(aktiven=True).order_by("naziv", "pk")
            if state.company_id
            else Tenant.objects.none()
        )
    return Lease.objects.filter(
        unit_id=state.unit_id,
        tenant_id=state.tenant_id,
        arhivirana=False,
    ).order_by("oznaka_pogodbe", "pk")


def _select(state, step, record):
    if getattr(state, f"{step}_id") == record.pk:
        return
    setattr(state, step, record)
    clear = {
        "company": ("property", "unit", "tenant", "lease"),
        "property": ("unit", "lease"),
        "unit": ("lease",),
        "tenant": ("lease",),
        "lease": (),
    }[step]
    for field in clear:
        setattr(state, field, None)
    state.save(update_fields=[step, *clear])


def _archive_status():
    config = ArchiveConfiguration.objects.filter(pk=1).first()
    verified = False
    error = "Lokacije shranjevanja še niso preverjene."
    if config:
        try:
            validate_archive_locations(
                config.local_root, config.icloud_root, config.dropbox_root
            )
        except ArchiveConfigurationError as exc:
            error = str(exc)
        else:
            verified = bool(config.verified_at)
    locations = [
        {
            "label": label,
            "path": getattr(config, field, ""),
            "verified_at": config.verified_at if verified else None,
        }
        for field, label in (
            ("local_root", "Lokalni arhiv"),
            ("icloud_root", "iCloud arhiv"),
            ("dropbox_root", "Dropbox varnostne kopije"),
        )
    ]
    return locations, [] if verified else [error]


def _links(state, step):
    create = edit = None
    record = getattr(state, step, None)
    routes = {
        "company": ("podjetje_novo", "podjetje_detail"),
        "property": ("nepremicnina_nova", "nepremicnina_detail"),
        "tenant": ("najemnik_nov", "najemnik_detail"),
        "lease": ("pogodba_nova", "pogodba_detail"),
    }
    if step in routes:
        create = reverse("ui:" + routes[step][0])
        if record:
            edit = reverse("ui:" + routes[step][1], args=[record.pk])
    elif step == "unit":
        if state.property_id:
            create = reverse("ui:enota_nova", args=[state.property_id])
        if state.unit_id:
            edit = reverse("ui:enota_detail", args=[state.unit_id])
    elif step == "items" and state.lease_id:
        create = reverse("ui:postavka_nova", args=[state.lease_id])
        edit = reverse("ui:pogodba_detail", args=[state.lease_id])
    return create, edit


@require_http_methods(["GET", "POST"])
@override("sl")
def first_run_setup(request):
    state = _navigation()
    step = request.GET.get("korak", state.step)
    if step not in dict(STEPS):
        return render(request, "ui/setup_error.html", status=400)
    if state.step != step:
        state.step = step
        state.save(update_fields=["step"])
    today = timezone.localdate()
    position = list(dict(STEPS)).index(step)
    form = None
    action_errors = []
    if step in {"company", "property", "unit", "tenant", "lease"}:
        form = SetupSelectionForm(
            request.POST
            if request.method == "POST" and request.POST.get("action") == "select"
            else None,
            queryset=_choices(state, step),
            label=dict(STEPS)[step],
            selected=getattr(state, step),
        )
        if form.is_bound and form.is_valid():
            record = form.cleaned_data["record"]
            _select(state, step, record)
            selection_errors = (
                [
                    message
                    for field, message in issuer_setup_issues(record, today)
                    if field not in {"signatory", "numbering"}
                ]
                if step == "company"
                else record_setup_errors(record)
            )
            if not selection_errors:
                state.step = STEPS[position + 1][0]
                state.save(update_fields=["step"])
                return redirect("ui:first_run_setup")
            action_errors = [
                "Izbrani zapis ni popoln. Uredite podatke pred nadaljevanjem."
            ]
    locations, archive_errors = _archive_status()
    issuer_issues = (
        issuer_setup_issues(state.company, today)
        if state.company
        else [("company", "Najprej izberite podjetje.")]
    )
    ddv = veljavna_potrditev_ddv(state.company) if state.company else None
    sample = veljavna_potrditev_vzorca(state.company) if state.company else None
    errors = {
        "company": [
            message
            for field, message in issuer_issues
            if field not in {"signatory", "numbering"}
        ]
        if state.company
        else ["Izberite ali dodajte podjetje."],
        "property": record_setup_errors(state.property),
        "unit": record_setup_errors(state.unit),
        "tenant": record_setup_errors(state.tenant),
        "lease": record_setup_errors(state.lease),
        "items": lease_items_setup_errors(state.lease, today),
        "issuer": [message for _, message in issuer_issues] + archive_errors,
    }
    for result, label in ((ddv, "Potrditev DDV"), (sample, "Potrditev vzorca")):
        if result is None or result.stanje != "valid":
            errors["issuer"].append(
                f"{label} {'je zastarela' if result and result.stanje == 'stale' else 'manjka'}."
            )
    if request.method == "POST" and request.POST.get("action") == "next":
        if errors[step]:
            action_errors = [
                "Korak še ni preverjen. Odpravite spodaj navedene manjkajoče podatke."
            ]
        elif position + 1 < len(STEPS):
            state.step = STEPS[position + 1][0]
            state.save(update_fields=["step"])
            return redirect("ui:first_run_setup")
    elif request.method == "POST" and (
        request.POST.get("action") not in {"select", "next"}
        or (request.POST.get("action") == "select" and form is None)
    ):
        action_errors = ["Neveljavno dejanje začetne nastavitve."]
    create_url, edit_url = _links(state, step)
    companies = [
        {
            "company": company,
            "ddv": veljavna_potrditev_ddv(company),
            "sample": veljavna_potrditev_vzorca(company),
            "issues": issuer_setup_issues(company, today),
        }
        for company in Company.objects.filter(aktivno=True).order_by("naziv", "pk")
    ]
    response = render(
        request,
        "ui/setup.html",
        {
            "active_section": "setup",
            "state": state,
            "step": step,
            "step_label": dict(STEPS)[step],
            "steps": [
                {"key": key, "label": label, "checked": not errors[key]}
                for key, label in STEPS
            ],
            "checked_count": sum(not values for values in errors.values()),
            "step_number": position + 1,
            "form": form,
            "missing": errors[step],
            "action_errors": action_errors,
            "previous_step": STEPS[position - 1][0] if position else None,
            "next_step": STEPS[position + 1][0] if position + 1 < len(STEPS) else None,
            "create_url": create_url,
            "edit_url": edit_url,
            "companies": companies,
            "locations": locations,
            "items": state.lease.leaseitem_set.filter(arhivirana=False)
            if state.lease
            else (),
        },
    )
    response["Cache-Control"] = "no-store"
    return response
