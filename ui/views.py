from calendar import monthrange
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import Count
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from catalogs.models import ServiceCatalogItem, VatRegime
from catalogs.services import (
    deaktiviraj_ddv_rezim,
    deaktiviraj_storitev,
    ponovno_aktiviraj_ddv_rezim,
    ponovno_aktiviraj_storitev,
)
from companies.models import Company, Signatory
from companies.services import (
    AssetPotrditveSpremenjen,
    arhiviraj_poslovni_zapis,
    ponovno_aktiviraj_poslovni_zapis,
    potrdi_ddv_nastavitve,
    potrdi_vzorec_podjetja,
    pregled_ddv_razporeditve,
    veljavna_potrditev_ddv,
    veljavna_potrditev_vzorca,
)
from contracts.models import Lease, LeaseItem, Property, RecipientContact, Tenant, Unit
from contracts.services import (
    arhiviraj_poslovni_zapis as arhiviraj_pogodbeni_zapis,
)
from contracts.services import (
    deaktiviraj_prejemnika,
    dnevi_v_obdobju,
    efektivna_veljavnost_pogodbe,
    hierarhija_pogodbe_je_uporabna,
    ponovno_aktiviraj_prejemnika,
    rocno_popravi_postavko,
)
from contracts.services import (
    ponovno_aktiviraj_poslovni_zapis as ponovno_aktiviraj_pogodbeni_zapis,
)
from invoicing.models import Invoice, MonthlyBatch, MonthlyCycle
from invoicing.services import (
    MesecniCikelNapaka,
    aktivni_izracuni_mesecnega_cikla,
    izdaj_zamrznjeni_mesecni_paket,
    izracunaj_mesecne_kandidate,
    nalozi_aktivni_mesecni_cikel,
    nastavi_izbiro_kandidata,
    odpri_zakljucek_mesecnega_cikla,
    potrdi_izjemo_kandidata,
    premakni_mesecni_cikel,
    preveri_predhodno_kontrolo,
    shrani_prvi_korak,
    so_vsi_uspesni_izracuni_aktivni,
    stanje_zamrznjenega_mesecnega_kandidata,
    trenutni_mesecni_kandidat,
    trenutni_pregledi_mesecnega_cikla,
    uskladi_preglede_mesecnega_cikla,
    zamrzni_paket_za_potrditev,
)
from rendering.contexts import PdfRenderNapaka
from rendering.services import (
    generiraj_mesecni_predogled_pdf,
    render_sample_pdf,
    renderiraj_koncne_pdfje_mesecnega_paketa,
    stanje_koncnega_pdf,
)

from .forms import (
    CompanyForm,
    IzbiraMesecaForm,
    LeaseForm,
    LeaseItemCorrectionForm,
    LeaseItemCreateForm,
    LeaseItemDetailsForm,
    PropertyForm,
    RecipientContactForm,
    SampleConfirmationForm,
    SampleLeaseSelectionForm,
    ServiceCatalogItemForm,
    SignatoryForm,
    TenantForm,
    UnitForm,
    VatRegimeForm,
    obdobje_za_mesec,
)


def _contracts_context(**extra):
    return {"active_page": "pogodbe", "active_section": "pogodbe"} | extra


@require_GET
def pogodbe(request):
    leases = Lease.objects.select_related(
        "unit__property__company", "tenant"
    ).order_by("arhivirana", "oznaka_pogodbe", "pk")
    return render(request, "ui/pogodbe.html", _contracts_context(leases=leases))


@require_http_methods(["GET", "POST"])
def pogodba_nova(request):
    return _lease_form(request)


@require_http_methods(["GET", "POST"])
def pogodba_detail(request, lease_id):
    lease = get_object_or_404(
        Lease.objects.select_related("unit__property__company", "tenant"),
        pk=lease_id,
    )
    return _lease_form(request, lease)


def _lease_form(request, lease=None, action_errors=None, *, bind_post=True):
    form = LeaseForm(request.POST if bind_post and request.method == "POST" else None, instance=lease)
    if bind_post and request.method == "POST" and form.is_valid():
        lease = form.save()
        return redirect("ui:pogodba_detail", lease_id=lease.pk)
    items = (
        LeaseItem.objects.filter(lease=lease).select_related("vat_regime").order_by(
            "arhivirana", "naziv_postavke", "pk"
        )
        if lease
        else ()
    )
    contacts = (
        RecipientContact.objects.filter(lease=lease).order_by(
            "-aktiven", "vloga", "email", "pk"
        )
        if lease
        else ()
    )
    return render(request, "ui/pogodba_form.html", _contracts_context(
        form=form, lease=lease, items=items, contacts=contacts,
        action_errors=action_errors,
        hierarchy_usable=_lease_hierarchy_usable(lease) if lease else False,
    ))


def _lease_state(request, lease_id, service):
    lease = get_object_or_404(
        Lease.objects.select_related("unit__property__company", "tenant"),
        pk=lease_id,
    )
    result = service(lease)
    if result.uspesno:
        return redirect("ui:pogodba_detail", lease_id=lease.pk)
    return _lease_form(
        request,
        lease,
        [r.sporocilo for r in result.razlogi],
        bind_post=False,
    )


@require_POST
def pogodba_arhiviraj(request, lease_id):
    return _lease_state(request, lease_id, arhiviraj_pogodbeni_zapis)


@require_POST
def pogodba_reaktiviraj(request, lease_id):
    return _lease_state(request, lease_id, ponovno_aktiviraj_pogodbeni_zapis)


def _lease_hierarchy_usable(lease):
    return bool(lease and hierarhija_pogodbe_je_uporabna(lease))


@require_http_methods(["GET", "POST"])
def prejemnik_nov(request, lease_id):
    lease = get_object_or_404(
        Lease.objects.select_related("unit__property__company", "tenant"), pk=lease_id
    )
    form = RecipientContactForm(
        request.POST or None,
        initial={"email": lease.tenant.email} if request.method == "GET" else None,
    )
    form.instance.lease = lease
    form.instance.kanal = RecipientContact.Kanal.PDF
    usable = hierarhija_pogodbe_je_uporabna(lease)
    if request.method == "POST" and usable and form.is_valid():
        contact = form.save()
        return redirect("ui:prejemnik_detail", contact_id=contact.pk)
    return render(request, "ui/prejemnik_form.html", _contracts_context(
        form=form, contact=None, lease=lease,
        action_errors=None if usable else [
            "Prejemnika ni mogoče dodati, ker pogodba ali njen nadrejeni zapis ni aktiven."
        ],
    ))


@require_http_methods(["GET", "POST"])
def prejemnik_detail(request, contact_id):
    contact = get_object_or_404(
        RecipientContact.objects.select_related(
            "lease__unit__property__company", "lease__tenant"
        ), pk=contact_id
    )
    return _recipient_form(request, contact)


def _recipient_form(request, contact, action_errors=None, *, bind_post=True):
    form = RecipientContactForm(
        request.POST if bind_post and request.method == "POST" else None,
        instance=contact,
    )
    if bind_post and request.method == "POST" and form.is_valid():
        form.save()
        return redirect("ui:prejemnik_detail", contact_id=contact.pk)
    return render(request, "ui/prejemnik_form.html", _contracts_context(
        form=form, contact=contact, lease=contact.lease, action_errors=action_errors,
    ))


def _recipient_state(request, contact_id, service):
    contact = get_object_or_404(
        RecipientContact.objects.select_related(
            "lease__unit__property__company", "lease__tenant"
        ), pk=contact_id
    )
    result = service(contact)
    if result.uspesno:
        return redirect("ui:prejemnik_detail", contact_id=contact.pk)
    contact.refresh_from_db()
    return _recipient_form(
        request, contact, [reason.sporocilo for reason in result.razlogi], bind_post=False
    )


@require_POST
def prejemnik_deaktiviraj(request, contact_id):
    return _recipient_state(request, contact_id, deaktiviraj_prejemnika)


@require_POST
def prejemnik_reaktiviraj(request, contact_id):
    return _recipient_state(request, contact_id, ponovno_aktiviraj_prejemnika)


@require_http_methods(["GET", "POST"])
def postavka_nova(request, lease_id):
    lease = get_object_or_404(
        Lease.objects.select_related("unit__property__company", "tenant"),
        pk=lease_id,
    )
    form = LeaseItemCreateForm(request.POST or None, lease=lease)
    usable = _lease_hierarchy_usable(lease)
    if request.method == "POST" and usable and form.is_valid():
        item = form.save()
        return redirect("ui:postavka_detail", item_id=item.pk)
    return render(request, "ui/postavka_form.html", _contracts_context(
        form=form, item=None, lease=lease,
        action_errors=None if usable else [
            "Postavke ni mogoče dodati, ker pogodba ali njen nadrejeni zapis ni aktiven."
        ],
    ))


@require_http_methods(["GET", "POST"])
def postavka_detail(request, item_id):
    item = get_object_or_404(
        LeaseItem.objects.select_related(
            "lease__unit__property__company", "lease__tenant", "vat_regime"
        ),
        pk=item_id,
    )
    return _item_form(request, item)


def _item_form(request, item, action_errors=None, *, bind_post=True):
    form = LeaseItemDetailsForm(
        request.POST if bind_post and request.method == "POST" else None,
        instance=item,
    )
    if bind_post and request.method == "POST" and form.is_valid():
        form.save()
        return redirect("ui:postavka_detail", item_id=item.pk)
    return render(request, "ui/postavka_form.html", _contracts_context(
        form=form, item=item, lease=item.lease, action_errors=action_errors,
    ))


@require_http_methods(["GET", "POST"])
def postavka_popravek(request, item_id):
    item = get_object_or_404(
        LeaseItem.objects.select_related("lease"), pk=item_id
    )
    initial = {
        "znesek_neto_mesecno": item.znesek_neto_mesecno,
        "datum_zacetka_postavke": item.datum_zacetka_postavke,
        "datum_prenehanja_postavke": item.datum_prenehanja_postavke,
    }
    form = LeaseItemCorrectionForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            rocno_popravi_postavko(
                item,
                razlog=form.cleaned_data["razlog"],
                znesek_neto_mesecno=form.cleaned_data["znesek_neto_mesecno"],
                datum_zacetka_postavke=form.cleaned_data["datum_zacetka_postavke"],
                datum_prenehanja_postavke=form.cleaned_data[
                    "datum_prenehanja_postavke"
                ],
            )
        except ValidationError as error:
            for field, messages in getattr(
                error, "message_dict", {None: error.messages}
            ).items():
                form.add_error(field if field in form.fields else None, messages)
        else:
            return redirect("ui:postavka_detail", item_id=item.pk)
    return render(request, "ui/postavka_popravek.html", _contracts_context(
        form=form, item=item,
    ))


def _item_state(request, item_id, service):
    item = get_object_or_404(
        LeaseItem.objects.select_related("lease__unit__property__company", "lease__tenant"),
        pk=item_id,
    )
    result = service(item)
    if result.uspesno:
        return redirect("ui:postavka_detail", item_id=item.pk)
    return _item_form(
        request,
        item,
        [r.sporocilo for r in result.razlogi],
        bind_post=False,
    )


@require_POST
def postavka_arhiviraj(request, item_id):
    return _item_state(request, item_id, arhiviraj_pogodbeni_zapis)


@require_POST
def postavka_reaktiviraj(request, item_id):
    return _item_state(request, item_id, ponovno_aktiviraj_pogodbeni_zapis)


@require_GET
def nepremicnine(request):
    properties = Property.objects.select_related("company").order_by(
        "-aktivna", "naziv", "pk"
    )
    unit_counts = {
        row["property_id"]: row["count"]
        for row in Unit.objects.values("property_id").annotate(count=Count("pk"))
    }
    for property_ in properties:
        property_.unit_count = unit_counts.get(property_.pk, 0)
    return render(request, "ui/nepremicnine.html", _contracts_context(properties=properties))


@require_http_methods(["GET", "POST"])
def nepremicnina_nova(request):
    return _property_form(request)


@require_http_methods(["GET", "POST"])
def nepremicnina_detail(request, property_id):
    return _property_form(request, get_object_or_404(Property, pk=property_id))


def _property_form(request, property_=None, action_errors=None):
    form = PropertyForm(request.POST or None, instance=property_)
    if request.method == "POST" and form.is_valid():
        property_ = form.save()
        return redirect("ui:nepremicnina_detail", property_id=property_.pk)
    units = Unit.objects.filter(property=property_).order_by("-aktivna", "oznaka", "pk") if property_ else ()
    return render(request, "ui/nepremicnina_form.html", _contracts_context(
        form=form, property=property_, units=units, action_errors=action_errors,
    ))


def _property_state(request, property_id, service):
    property_ = get_object_or_404(Property, pk=property_id)
    result = service(property_)
    if result.uspesno:
        return redirect("ui:nepremicnina_detail", property_id=property_.pk)
    return _property_form(request, property_, [r.sporocilo for r in result.razlogi])


@require_POST
def nepremicnina_arhiviraj(request, property_id):
    return _property_state(request, property_id, arhiviraj_pogodbeni_zapis)


@require_POST
def nepremicnina_reaktiviraj(request, property_id):
    return _property_state(request, property_id, ponovno_aktiviraj_pogodbeni_zapis)


@require_http_methods(["GET", "POST"])
def enota_nova(request, property_id):
    property_ = get_object_or_404(
        Property.objects.select_related("company"), pk=property_id,
        aktivna=True, company__aktivno=True,
    )
    form = UnitForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        unit = form.save(commit=False)
        unit.property = property_
        unit.save()
        return redirect("ui:enota_detail", unit_id=unit.pk)
    return render(request, "ui/enota_form.html", _contracts_context(
        form=form, unit=None, property=property_,
    ))


@require_http_methods(["GET", "POST"])
def enota_detail(request, unit_id):
    unit = get_object_or_404(Unit.objects.select_related("property"), pk=unit_id)
    return _unit_form(request, unit)


def _unit_form(request, unit, action_errors=None):
    form = UnitForm(request.POST or None, instance=unit)
    if request.method == "POST" and form.is_valid():
        form.save()
        return redirect("ui:enota_detail", unit_id=unit.pk)
    return render(request, "ui/enota_form.html", _contracts_context(
        form=form, unit=unit, property=unit.property, action_errors=action_errors,
    ))


def _unit_state(request, unit_id, service):
    unit = get_object_or_404(Unit.objects.select_related("property"), pk=unit_id)
    result = service(unit)
    if result.uspesno:
        return redirect("ui:enota_detail", unit_id=unit.pk)
    return _unit_form(request, unit, [r.sporocilo for r in result.razlogi])


@require_POST
def enota_arhiviraj(request, unit_id):
    return _unit_state(request, unit_id, arhiviraj_pogodbeni_zapis)


@require_POST
def enota_reaktiviraj(request, unit_id):
    return _unit_state(request, unit_id, ponovno_aktiviraj_pogodbeni_zapis)


@require_GET
def najemniki(request):
    tenants = Tenant.objects.order_by("-aktiven", "naziv", "pk")
    return render(request, "ui/najemniki.html", _contracts_context(tenants=tenants))


@require_http_methods(["GET", "POST"])
def najemnik_nov(request):
    return _tenant_form(request)


@require_http_methods(["GET", "POST"])
def najemnik_detail(request, tenant_id):
    return _tenant_form(request, get_object_or_404(Tenant, pk=tenant_id))


def _tenant_form(request, tenant=None, action_errors=None):
    form = TenantForm(request.POST or None, instance=tenant)
    if request.method == "POST" and form.is_valid():
        tenant = form.save()
        return redirect("ui:najemnik_detail", tenant_id=tenant.pk)
    return render(request, "ui/najemnik_form.html", _contracts_context(
        form=form, tenant=tenant, action_errors=action_errors,
    ))


def _tenant_state(request, tenant_id, service):
    tenant = get_object_or_404(Tenant, pk=tenant_id)
    result = service(tenant)
    if result.uspesno:
        return redirect("ui:najemnik_detail", tenant_id=tenant.pk)
    return _tenant_form(request, tenant, [r.sporocilo for r in result.razlogi])


@require_POST
def najemnik_arhiviraj(request, tenant_id):
    return _tenant_state(request, tenant_id, arhiviraj_pogodbeni_zapis)


@require_POST
def najemnik_reaktiviraj(request, tenant_id):
    return _tenant_state(request, tenant_id, ponovno_aktiviraj_pogodbeni_zapis)


def _settings_context(**extra):
    return {"active_page": "nastavitve", "active_section": "nastavitve"} | extra


@require_GET
def nastavitve(request):
    companies = Company.objects.order_by("-aktivno", "naziv", "pk")
    return render(
        request,
        "ui/nastavitve.html",
        _settings_context(companies=companies),
    )


@require_GET
def ddv_rezimi(request):
    regimes = VatRegime.objects.order_by("-aktiven", "sifra", "pk")
    return render(
        request, "ui/ddv_rezimi.html", _settings_context(regimes=regimes)
    )


@require_http_methods(["GET", "POST"])
def ddv_rezim_nov(request):
    return _ddv_rezim_form(request)


@require_http_methods(["GET", "POST"])
def ddv_rezim_detail(request, regime_id):
    return _ddv_rezim_form(request, get_object_or_404(VatRegime, pk=regime_id))


def _ddv_rezim_form(request, regime=None):
    form = VatRegimeForm(request.POST or None, instance=regime)
    if request.method == "POST" and form.is_valid():
        regime = form.save()
        return redirect("ui:ddv_rezim_detail", regime_id=regime.pk)
    return render(
        request,
        "ui/catalog_form.html",
        _settings_context(
            form=form,
            object=regime,
            section="DDV režimi",
            title=regime.naziv if regime else "Nov DDV režim",
            description="Davčna pravila in prikaz na računu.",
            back_url="ui:ddv_rezimi",
            save_label="Shrani DDV režim",
            state_field="aktiven",
            deactivate_url="ui:ddv_rezim_deaktiviraj",
            activate_url="ui:ddv_rezim_aktiviraj",
        ),
    )


def _catalog_state(request, model, object_id, service, detail_name, form_view):
    obj = get_object_or_404(model, pk=object_id)
    result = service(obj.pk)
    if result.uspesno:
        return redirect(detail_name, obj.pk)
    return form_view(request, obj, [reason.sporocilo for reason in result.razlogi])


def _render_ddv_with_errors(request, regime, errors):
    form = VatRegimeForm(instance=regime)
    return render(
        request,
        "ui/catalog_form.html",
        _settings_context(
            form=form, object=regime, section="DDV režimi", title=regime.naziv,
            description="Davčna pravila in prikaz na računu.", back_url="ui:ddv_rezimi",
            save_label="Shrani DDV režim", state_field="aktiven",
            deactivate_url="ui:ddv_rezim_deaktiviraj", activate_url="ui:ddv_rezim_aktiviraj",
            action_errors=errors,
        ),
    )


@require_POST
def ddv_rezim_deaktiviraj(request, regime_id):
    return _catalog_state(request, VatRegime, regime_id, deaktiviraj_ddv_rezim, "ui:ddv_rezim_detail", _render_ddv_with_errors)


@require_POST
def ddv_rezim_aktiviraj(request, regime_id):
    return _catalog_state(request, VatRegime, regime_id, ponovno_aktiviraj_ddv_rezim, "ui:ddv_rezim_detail", _render_ddv_with_errors)


@require_GET
def storitve(request):
    items = ServiceCatalogItem.objects.select_related("privzeti_ddv_rezim").order_by(
        "-aktivna", "naziv", "pk"
    )
    return render(request, "ui/storitve.html", _settings_context(items=items))


@require_http_methods(["GET", "POST"])
def storitev_nova(request):
    return _storitev_form(request)


@require_http_methods(["GET", "POST"])
def storitev_detail(request, item_id):
    return _storitev_form(request, get_object_or_404(ServiceCatalogItem, pk=item_id))


def _storitev_form(request, item=None, action_errors=None):
    form = ServiceCatalogItemForm(request.POST or None, instance=item)
    if request.method == "POST" and form.is_valid():
        item = form.save()
        return redirect("ui:storitev_detail", item_id=item.pk)
    return render(
        request,
        "ui/catalog_form.html",
        _settings_context(
            form=form, object=item, section="Storitve",
            title=item.naziv if item else "Nova storitev",
            description="Privzete vrednosti za hitrejši vnos storitev.",
            back_url="ui:storitve", save_label="Shrani storitev",
            state_field="aktivna", deactivate_url="ui:storitev_deaktiviraj",
            activate_url="ui:storitev_aktiviraj", action_errors=action_errors,
        ),
    )


@require_POST
def storitev_deaktiviraj(request, item_id):
    return _catalog_state(request, ServiceCatalogItem, item_id, deaktiviraj_storitev, "ui:storitev_detail", _storitev_form)


@require_POST
def storitev_aktiviraj(request, item_id):
    return _catalog_state(request, ServiceCatalogItem, item_id, ponovno_aktiviraj_storitev, "ui:storitev_detail", _storitev_form)


@require_http_methods(["GET", "POST"])
def podjetje_novo(request):
    form = CompanyForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        company = form.save()
        return redirect("ui:podjetje_detail", company_id=company.pk)
    return render(
        request,
        "ui/podjetje_form.html",
        _settings_context(form=form, company=None),
    )


@require_http_methods(["GET", "POST"])
def podjetje_detail(request, company_id):
    company = get_object_or_404(Company, pk=company_id)
    selected_sample_lease = None
    if request.method == "GET" and request.GET.get("vzorec_pogodba"):
        selected_sample_lease = _sample_lease_or_404(
            company, request.GET["vzorec_pogodba"]
        )
    form = CompanyForm(request.POST or None, request.FILES or None, instance=company)
    if request.method == "POST" and form.is_valid():
        form.save()
        return redirect("ui:podjetje_detail", company_id=company.pk)
    return render(
        request,
        "ui/podjetje_detail.html",
        _company_detail_context(
            company,
            form=form,
            confirmation_marker=request.GET.get("potrditev"),
            selected_sample_lease=selected_sample_lease,
        ),
    )


def _confirmation_success(marker, ddv_state, sample_state):
    if marker == "ddv" and ddv_state.stanje == "valid":
        return "ddv"
    if marker == "vzorec" and sample_state.stanje == "valid":
        return "vzorec"
    return None


def _company_detail_context(
    company,
    *,
    form=None,
    action_errors=None,
    confirmation_marker=None,
    selected_sample_lease=None,
):
    ddv_state = veljavna_potrditev_ddv(company)
    sample_state = veljavna_potrditev_vzorca(company)
    return _settings_context(
        company=company,
        form=form or CompanyForm(instance=company),
        signatories=company.signatory_set.order_by("-velja_od", "-pk"),
        ddv_state=ddv_state,
        sample_state=sample_state,
        ddv_allocations=pregled_ddv_razporeditve(company),
        sample_selection_form=SampleLeaseSelectionForm(
            company=company,
            initial={"vzorec_pogodba": selected_sample_lease},
        ),
        selected_sample_lease=selected_sample_lease,
        confirmation_success=_confirmation_success(
            confirmation_marker, ddv_state, sample_state
        ),
        action_errors=action_errors,
    )


@require_POST
def podjetje_potrdi_ddv(request, company_id):
    company = get_object_or_404(Company, pk=company_id)
    potrdi_ddv_nastavitve(company)
    response = redirect("ui:podjetje_detail", company_id=company.pk)
    response["Location"] += "?potrditev=ddv"
    return response


def _sample_lease_or_404(company, lease_id):
    form = SampleConfirmationForm({"lease": lease_id}, company=company)
    if not form.is_valid():
        from django.http import Http404

        raise Http404("Izbrana pogodba ni na voljo za vzorec.")
    return form.cleaned_data["lease"]


@require_GET
def podjetje_vzorec_pdf(request, company_id, lease_id):
    company = get_object_or_404(Company, pk=company_id)
    lease = _sample_lease_or_404(company, lease_id)
    danes = date.today()  # noqa: DTZ011
    try:
        rezultat = render_sample_pdf(
            lease,
            datum_izdaje=danes,
            obdobje_od=danes.replace(day=1),
            obdobje_do=danes.replace(day=monthrange(danes.year, danes.month)[1]),
        )
    except (PdfRenderNapaka, ValidationError, ValueError, OSError) as napaka:
        return HttpResponse(str(napaka), status=409, content_type="text/plain")
    response = HttpResponse(rezultat.pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = 'inline; filename="VZOREC-NI-RACUN.pdf"'
    response["Cache-Control"] = "no-store"
    return response


@require_POST
def podjetje_potrdi_vzorec(request, company_id):
    company = get_object_or_404(Company, pk=company_id)
    lease = _sample_lease_or_404(company, request.POST.get("lease"))
    try:
        potrdi_vzorec_podjetja(company, lease)
    except (AssetPotrditveSpremenjen, PdfRenderNapaka, ValidationError, ValueError, OSError) as napaka:
        return render(
            request,
            "ui/podjetje_detail.html",
            _company_detail_context(
                company,
                action_errors=[str(napaka)],
                selected_sample_lease=lease,
            ),
            status=409,
        )
    response = redirect("ui:podjetje_detail", company_id=company.pk)
    response["Location"] += "?potrditev=vzorec"
    return response


def _stanje_podjetja(request, company_id, service):
    company = get_object_or_404(Company, pk=company_id)
    rezultat = service(company)
    if rezultat.uspesno:
        return redirect("ui:podjetje_detail", company_id=company.pk)
    form = CompanyForm(instance=company)
    return render(
        request,
        "ui/podjetje_detail.html",
        _company_detail_context(
            company,
            form=form,
            action_errors=[razlog.sporocilo for razlog in rezultat.razlogi],
        ),
    )


@require_POST
def podjetje_arhiviraj(request, company_id):
    return _stanje_podjetja(request, company_id, arhiviraj_poslovni_zapis)


@require_POST
def podjetje_reaktiviraj(request, company_id):
    return _stanje_podjetja(request, company_id, ponovno_aktiviraj_poslovni_zapis)


@require_http_methods(["GET", "POST"])
def podpisnik_nov(request, company_id):
    company = get_object_or_404(Company, pk=company_id)
    form = SignatoryForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        signatory = form.save(commit=False)
        signatory.company = company
        try:
            signatory.full_clean()
        except ValidationError as error:
            form.add_error(None, error)
        else:
            signatory.save()
            return redirect("ui:podjetje_detail", company_id=company.pk)
    return render(
        request,
        "ui/podpisnik_form.html",
        _settings_context(form=form, company=company, signatory=None),
    )


@require_http_methods(["GET", "POST"])
def podpisnik_detail(request, signatory_id):
    signatory = get_object_or_404(Signatory.objects.select_related("company"), pk=signatory_id)
    form = SignatoryForm(request.POST or None, request.FILES or None, instance=signatory)
    if request.method == "POST" and form.is_valid():
        form.save()
        return redirect("ui:podjetje_detail", company_id=signatory.company_id)
    return render(
        request,
        "ui/podpisnik_form.html",
        _settings_context(form=form, company=signatory.company, signatory=signatory),
    )


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
    confirmation_tasks = []
    for company in Company.objects.filter(aktivno=True).order_by("naziv", "pk"):
        ddv_state = veljavna_potrditev_ddv(company)
        sample_state = veljavna_potrditev_vzorca(company)
        if ddv_state.stanje != "valid" or sample_state.stanje != "valid":
            confirmation_tasks.append((company, ddv_state, sample_state))
    return render(
        request,
        "ui/dashboard.html",
        {
            "counts": counts,
            "active_page": "dashboard",
            "active_section": "dashboard",
            "aktivni_cikel": aktivni_cikel,
            "confirmation_tasks": confirmation_tasks,
        },
    )


@require_GET
def mesecni_obracun_predogled_pdf(request):
    try:
        lease_id = int(request.GET.get("lease_id", ""))
        fingerprint = request.GET.get("candidate_fingerprint", "").strip()
        if not fingerprint:
            raise ValidationError("Manjka identiteta kandidata.")
        cycle = nalozi_aktivni_mesecni_cikel()
        if cycle is None:
            raise ValidationError("Aktivni mesečni obračun ne obstaja.")
        batch, candidate = trenutni_mesecni_kandidat(
            cycle, lease_id=lease_id, candidate_fingerprint=fingerprint
        )
        pdf_bytes = generiraj_mesecni_predogled_pdf(
            batch.calculation_snapshot, candidate
        )
    except PdfRenderNapaka as napaka:
        status = 422 if napaka.koda != "PDF_GENERIRANJE_NI_USPELO" else 500
        sporocilo = (
            "Predogleda PDF ni bilo mogoče ustvariti."
            if status == 500
            else str(napaka)
        )
        return HttpResponse(sporocilo, status=status, content_type="text/plain")
    except (TypeError, ValueError):
        return HttpResponse("ID pogodbe ni veljaven.", status=400, content_type="text/plain")
    except MesecniCikelNapaka as napaka:
        return HttpResponse(str(napaka), status=409, content_type="text/plain")
    except ValidationError as napaka:
        sporocilo = napaka.messages[0]
        status = 404 if sporocilo == "Kandidat ni trenutni aktivni kandidat." else 409
        if status == 404:
            sporocilo = "Predogled za tega kandidata ni več na voljo."
        return HttpResponse(sporocilo, status=status, content_type="text/plain")
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = 'inline; filename="predogled-racuna.pdf"'
    response["Cache-Control"] = "no-store"
    return response


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
    if request.method == "POST" and action == "freeze_package" and cycle:
        try:
            cycle = zamrzni_paket_za_potrditev(cycle)
        except (MesecniCikelNapaka, ValidationError) as exc:
            review_error = _review_error_message(exc)
        else:
            return redirect("ui:mesecni_obracun")
    if request.method == "POST" and action == "issue_frozen_package" and cycle:
        try:
            izdaj_zamrznjeni_mesecni_paket(cycle)
        except MesecniCikelNapaka as exc:
            review_error = _review_error_message(exc)
        return redirect("ui:mesecni_obracun")
    if request.method == "POST" and action == "render_final_pdfs" and cycle:
        try:
            results = renderiraj_koncne_pdfje_mesecnega_paketa(cycle)
            failures = [result for result in results if not result["ok"]]
            if failures:
                failed = failures[0]
                review_error_identity = (
                    failed["lease_id"], failed["candidate_fingerprint"]
                )
                review_error = failed["error"]
            else:
                odpri_zakljucek_mesecnega_cikla(cycle)
                return redirect("ui:mesecni_obracun")
        except (MesecniCikelNapaka, ValidationError) as exc:
            review_error = _review_error_message(exc)

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
    if cycle and cycle.trenutni_korak in (
        MonthlyCycle.Korak.PAKETNI_PREGLED,
        MonthlyCycle.Korak.POTRDITEV_PAKETA,
        MonthlyCycle.Korak.ZAKLJUCEK,
    ):
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
    final_render_can_attempt = bool(
        cycle
        and cycle.trenutni_korak == MonthlyCycle.Korak.POTRDITEV_PAKETA
        and all(
            row["materialization"]["state"] == "MATERIALIZED"
            for row in review_rows
        )
    )
    step6_all_pdfs_ready = bool(
        cycle
        and cycle.trenutni_korak == MonthlyCycle.Korak.ZAKLJUCEK
        and all(
            not row["selected"]
            or (
                row["pdf_state"] is not None
                and row["pdf_state"]["state"] == "READY"
            )
            for row in review_rows
        )
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
            "batches": batches,
            "preflight_summary": summary,
            "step2_all_passed": bool(summary and summary["passed"] == summary["total"]),
            "step2_partial": bool(summary and summary["passed"] and summary["blocked"]),
            "step3_complete": step3_complete,
            "step3_can_continue": step3_complete,
            "review_rows": review_rows,
            "review_summary": review_summary,
            "review_error": review_error,
            "final_render_can_attempt": final_render_can_attempt,
            "step6_all_pdfs_ready": step6_all_pdfs_ready,
            "step4_complete": bool(
                cycle
                and cycle.trenutni_korak >= MonthlyCycle.Korak.POTRDITEV_PAKETA
            ),
            "step5_complete": bool(
                cycle and cycle.trenutni_korak >= MonthlyCycle.Korak.ZAKLJUCEK
            ),
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
                    "materialization": stanje_zamrznjenega_mesecnega_kandidata(
                        batch.pk,
                        lease_id,
                        fingerprint,
                    ) if cycle.trenutni_korak >= MonthlyCycle.Korak.POTRDITEV_PAKETA
                    else {"state": "PENDING", "invoice": None, "error": None},
                }
            )
            materialization = rows[-1]["materialization"]
            if review.selected and materialization["state"] == "MATERIALIZED":
                rows[-1]["pdf_state"] = stanje_koncnega_pdf(materialization["invoice"])
            else:
                rows[-1]["pdf_state"] = None
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
