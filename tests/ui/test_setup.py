from datetime import date
from decimal import Decimal

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from archive.configuration import verify_and_save_archive_roots
from archive.models import ArchiveConfiguration, ArchivedFile, BackupSnapshot
from companies.models import (
    Company,
    CompanySampleConfirmation,
    NumberSequence,
    VatSettingsConfirmation,
)
from companies.services import potrdi_ddv_nastavitve, potrdi_vzorec_podjetja
from companies.setup import issuer_setup_issues
from contracts.models import Lease, LeaseItem, Property, Tenant, Unit
from delivery.models import DeliveryAttempt
from invoicing.models import Invoice, InvoiceLine, MonthlyBatch, MonthlyCycle
from tests.ui import test_company_confirmations
from tests.ui.test_nastavitve import company_data
from ui.models import SetupNavigation
from ui.setup import STEPS

pytestmark = pytest.mark.django_db
setup_data = test_company_confirmations.confirmation_ui_data
URL = "/zacetna-nastavitev/"


def select(client, step, record):
    return client.post(
        URL + f"?korak={step}", {"action": "select", "record": record.pk}, follow=True
    )


def select_chain(client, data):
    lease = data.lease
    for step, record in (
        ("company", data.company),
        ("property", lease.unit.property),
        ("unit", lease.unit),
        ("tenant", lease.tenant),
        ("lease", lease),
    ):
        assert select(client, step, record).status_code == 200


def test_new_install_has_all_steps_missing_and_create_links(client):
    response = client.get(URL)
    assert response.status_code == 200
    assert response.context["checked_count"] == 0
    assert response.context["step"] == "company"
    content = response.content.decode()
    assert all(label in content for _, label in STEPS)
    assert "Podjetja še niso vnesena" in content
    assert "Začetna nastavitev še ni zaključena" in content
    assert reverse("ui:podjetje_novo") in content
    assert SetupNavigation.objects.count() == 1
    assert not Company.objects.exists()
    assert URL in client.get("/").content.decode()


def test_missing_selection_does_not_advance(client):
    response = client.post(URL, {"action": "select", "record": ""})
    assert "Izberite zapis ali ga najprej dodajte" in response.content.decode()
    response = client.post(URL, {"action": "next"})
    assert "Korak še ni preverjen" in response.content.decode()
    assert SetupNavigation.objects.get().step == "company"


def test_existing_records_never_invent_confirmation(client, setup_data):
    response = client.get(URL)
    row = response.context["companies"][0]
    assert row["ddv"].stanje == row["sample"].stanje == "missing"
    select_chain(client, setup_data)
    response = client.get(URL + "?korak=issuer")
    assert "Potrditev DDV manjka" in response.content.decode()
    assert "Potrditev vzorca manjka" in response.content.decode()
    assert not VatSettingsConfirmation.objects.exists()
    assert not CompanySampleConfirmation.objects.exists()


def test_five_arbitrary_issuers_have_independent_status(client, setup_data):
    companies = [setup_data.company]
    for index in range(4):
        values = {
            field.name: getattr(setup_data.company, field.name)
            for field in Company._meta.fields
            if field.name != "id"
        }
        values.update(naziv=f"Izdajatelj {index}", davcna_stevilka=f"9000000{index}")
        companies.append(Company.objects.create(**values))
    potrdi_ddv_nastavitve(companies[2])
    response = client.get(URL)
    rows = {row["company"].pk: row for row in response.context["companies"]}
    assert set(rows) == {company.pk for company in companies}
    assert rows[companies[2].pk]["ddv"].stanje == "valid"
    assert all(row["sample"].stanje == "missing" for row in rows.values())
    assert "Vnesena aktivna podjetja: 5" in response.content.decode()


@pytest.mark.parametrize(
    "field,value",
    [
        ("trr", ""),
        ("trr", "invalid"),
        ("email_posiljatelj", ""),
        ("email_posiljatelj", "invalid"),
        ("email_racunovodja", ""),
        ("logotip", "missing.png"),
        ("logotip", "../outside.png"),
    ],
)
def test_missing_or_invalid_issuer_configuration(client, setup_data, field, value):
    Company.objects.filter(pk=setup_data.company.pk).update(**{field: value})
    setup_data.company.refresh_from_db()
    issues = issuer_setup_issues(setup_data.company, timezone.localdate())
    assert any(key == field for key, _ in issues)
    select(client, "company", setup_data.company)
    response = client.get(URL + "?korak=issuer")
    assert response.context["missing"]
    assert (
        reverse("ui:podjetje_detail", args=[setup_data.company.pk])
        in response.content.decode()
    )


def test_signatory_signature_and_numbering_are_required(setup_data):
    company = setup_data.company
    today = timezone.localdate()
    assert (
        sum(key == "numbering" for key, _ in issuer_setup_issues(company, today)) == 2
    )
    for tip, year in (("NP", 0), ("ST", today.year)):
        NumberSequence.objects.create(
            company=company, tip=tip, leto=year, zadnja_stevilka=17
        )
    assert not issuer_setup_issues(company, today)
    company.signatory_set.update(podpis_slika="missing.png")
    assert any(
        "Slika podpisa" in message for _, message in issuer_setup_issues(company, today)
    )
    company.signatory_set.update(velja_od=date(2099, 1, 1))
    assert any(
        "Ni veljavnega podpisnika" in message
        for _, message in issuer_setup_issues(company, today)
    )


def test_navigation_browser_restart_and_new_server_state(client, setup_data):
    select_chain(client, setup_data)
    response = client.get(URL)
    assert response.context["step"] == "items"
    assert not response.context["missing"]
    response = client.post(URL, {"action": "next"}, follow=True)
    assert response.context["step"] == "issuer"
    restarted = Client()
    response = restarted.get(URL)
    assert response.context["step"] == "issuer"
    assert response.context["state"].lease_id == setup_data.lease.pk
    assert restarted.cookies.get("sessionid") is None
    response = restarted.get(URL + "?korak=unit")
    assert response.context["previous_step"] == "property"
    assert response.context["next_step"] == "tenant"
    assert Client().get(URL).context["step"] == "unit"
    assert (
        select(restarted, "company", setup_data.company).context["state"].lease_id
        == setup_data.lease.pk
    )


def test_invalid_step_and_action_preserve_navigation(client, setup_data):
    select_chain(client, setup_data)
    assert client.get(URL + "?korak=unknown").status_code == 400
    assert SetupNavigation.objects.get().step == "items"
    assert (
        "Neveljavno dejanje" in client.post(URL, {"action": "finish"}).content.decode()
    )
    assert (
        "Neveljavno dejanje" in client.post(URL, {"action": "select"}).content.decode()
    )


def test_csrf_and_methods(client):
    assert (
        Client(enforce_csrf_checks=True).post(URL, {"action": "next"}).status_code
        == 403
    )
    assert client.put(URL).status_code == 405


def test_selection_scoped_and_company_change_clears_chain(client, setup_data):
    select_chain(client, setup_data)
    other = Company.objects.create(naziv="Drugo podjetje", davcna_stevilka="25012550")
    property_ = Property.objects.create(
        company=other, naziv="Tuja stavba", naslov="Naslov"
    )
    response = select(client, "property", property_)
    assert "Izbrani zapis ni aktiven" in response.content.decode()
    assert (
        SetupNavigation.objects.get().property_id == setup_data.lease.unit.property_id
    )
    select(client, "company", other)
    state = SetupNavigation.objects.get()
    assert all(
        getattr(state, f"{field}_id") is None
        for field in ("property", "unit", "tenant", "lease")
    )


@pytest.mark.parametrize(
    "model,field",
    [
        (Property, "aktivna"),
        (Unit, "aktivna"),
        (Tenant, "aktiven"),
        (Lease, "arhivirana"),
    ],
)
def test_stale_archived_selections_reloaded(client, setup_data, model, field):
    select_chain(client, setup_data)
    record = {
        Property: setup_data.lease.unit.property,
        Unit: setup_data.lease.unit,
        Tenant: setup_data.lease.tenant,
        Lease: setup_data.lease,
    }[model]
    model.objects.filter(pk=record.pk).update(**{field: field == "arhivirana"})
    response = Client().get(URL)
    assert response.context["state"].lease_id is None
    assert response.context["missing"]


def test_incomplete_chain_and_existing_validations(client, setup_data):
    select_chain(client, setup_data)
    Tenant.objects.filter(pk=setup_data.lease.tenant_id).update(email="bad")
    response = client.get(URL + "?korak=tenant")
    assert response.context["missing"]
    assert "veljaven e-poštni naslov" in response.content.decode().lower()
    LeaseItem.objects.filter(pk=setup_data.item.pk).update(arhivirana=True)
    response = client.get(URL + "?korak=items")
    assert "Pogodba nima aktivnih pogodbenih postavk" in response.content.decode()
    LeaseItem.objects.filter(pk=setup_data.item.pk).update(
        arhivirana=False, znesek_neto_mesecno=Decimal("0.00")
    )
    assert client.get(URL).context["missing"]


def test_stale_confirmations_are_derived_again(client, setup_data):
    potrdi_ddv_nastavitve(setup_data.company)
    potrdi_vzorec_podjetja(setup_data.company, setup_data.lease)
    select_chain(client, setup_data)
    rows = client.get(URL + "?korak=issuer").context["companies"]
    assert rows[0]["ddv"].stanje == rows[0]["sample"].stanje == "valid"
    setup_data.regime.stopnja = Decimal("9.50")
    setup_data.regime.save(update_fields=["stopnja"])
    response = Client().get(URL)
    assert response.context["companies"][0]["ddv"].stanje == "stale"
    assert response.context["companies"][0]["sample"].stanje == "stale"
    assert "Potrditev DDV je zastarela" in response.content.decode()


@pytest.mark.django_db(transaction=True)
def test_existing_three_location_verification_and_legacy_config(
    client, setup_data, tmp_path
):
    select(client, "company", setup_data.company)
    roots = [
        tmp_path.parent / f"{tmp_path.name}-{name}"
        for name in ("local", "icloud", "dropbox")
    ]
    for root in roots:
        root.mkdir()
    config = verify_and_save_archive_roots(*roots)
    response = client.get(URL + "?korak=issuer")
    assert all(
        location["verified_at"] == config.verified_at
        for location in response.context["locations"]
    )
    assert all(not list(root.iterdir()) for root in roots)
    ArchiveConfiguration.objects.update(dropbox_root="")
    assert all(
        location["verified_at"] is None
        for location in client.get(URL).context["locations"]
    )


def test_no_invoice_numbering_delivery_archive_or_approval_side_effects(
    client, setup_data, monkeypatch
):
    select_chain(client, setup_data)
    NumberSequence.objects.create(
        company=setup_data.company, tip="NP", leto=0, zadnja_stevilka=42
    )
    models = (
        Invoice,
        InvoiceLine,
        MonthlyCycle,
        MonthlyBatch,
        NumberSequence,
        DeliveryAttempt,
        ArchivedFile,
        BackupSnapshot,
        VatSettingsConfirmation,
        CompanySampleConfirmation,
    )
    before = [list(model.objects.values()) for model in models]

    def forbidden(*args, **kwargs):
        pytest.fail("Wizard triggered invoice or filesystem side effects")

    for target in (
        "rendering.services.render_sample_pdf",
        "companies.services.dodeli_naslednjo_stevilko",
        "archive.configuration.verify_and_save_archive_roots",
    ):
        monkeypatch.setattr(target, forbidden)
    for step, _ in STEPS:
        assert client.get(URL + f"?korak={step}").status_code == 200
    client.post(URL, {"action": "next"})
    client.post(URL, {"action": "finish"})
    assert [list(model.objects.values()) for model in models] == before
    assert {field.name for field in SetupNavigation._meta.fields} == {
        "id",
        "step",
        "company",
        "property",
        "unit",
        "tenant",
        "lease",
    }


def test_create_edit_links_use_existing_forms(client, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    create_url = client.get(URL).context["create_url"]
    assert client.post(create_url, company_data()).status_code == 302
    company = Company.objects.get()
    select(client, "company", company)
    response = client.get(URL + "?korak=company")
    assert response.context["edit_url"] == reverse(
        "ui:podjetje_detail", args=[company.pk]
    )
    assert client.get(response.context["edit_url"]).status_code == 200
    assert SetupNavigation.objects.get().company_id == company.pk


def test_legacy_company_without_name_renders_and_stays_incomplete(client, setup_data):
    Company.objects.filter(pk=setup_data.company.pk).update(naziv="")
    response = client.get(URL)
    assert response.status_code == 200
    assert "manjka naziv" in response.content.decode()
    response = select(client, "company", setup_data.company)
    assert response.context["step"] == "company"
    assert response.context["missing"]
    assert not response.context["steps"][0]["checked"]
    assert response.context["edit_url"]


def test_select_incomplete_tenant_keeps_edit_link_and_current_step(client, setup_data):
    select_chain(client, setup_data)
    Tenant.objects.filter(pk=setup_data.lease.tenant_id).update(email="invalid")
    response = select(client, "tenant", setup_data.lease.tenant)
    assert response.context["step"] == "tenant"
    assert response.context["missing"]
    assert response.context["edit_url"] == reverse(
        "ui:najemnik_detail", args=[setup_data.lease.tenant_id]
    )
    assert SetupNavigation.objects.get().lease_id == setup_data.lease.pk


def test_ended_lease_cannot_pass_lease_or_items_step(client, setup_data):
    select_chain(client, setup_data)
    Lease.objects.filter(pk=setup_data.lease.pk).update(status=Lease.Status.PRENEHALA)
    response = select(client, "lease", setup_data.lease)
    assert response.context["step"] == "lease"
    assert "Izbrana pogodba ni aktivna" in response.content.decode()
    response = client.post(URL + "?korak=items", {"action": "next"})
    assert response.context["missing"]
    assert response.context["step"] == "items"


def test_reparented_unit_invalidates_saved_chain(client, setup_data):
    select_chain(client, setup_data)
    other_property = Property.objects.create(
        company=setup_data.company, naziv="Druga stavba", naslov="B 2"
    )
    Unit.objects.filter(pk=setup_data.lease.unit_id).update(property=other_property)
    state = Client().get(URL).context["state"]
    assert state.unit_id is None
    assert state.lease_id is None
    assert state.property_id == setup_data.lease.unit.property_id


def test_archived_company_clears_all_selections_on_resume(client, setup_data):
    select_chain(client, setup_data)
    Company.objects.filter(pk=setup_data.company.pk).update(aktivno=False)
    response = Client().get(URL)
    state = response.context["state"]
    assert all(
        getattr(state, f"{field}_id") is None
        for field in ("company", "property", "unit", "tenant", "lease")
    )
    assert not response.context["companies"]


def test_whitespace_legacy_property_is_not_checked(client, setup_data):
    select_chain(client, setup_data)
    Property.objects.filter(pk=setup_data.lease.unit.property_id).update(naziv="   ")
    response = client.get(URL + "?korak=property")
    assert "Manjka obvezen podatek" in response.content.decode()
    assert not response.context["steps"][1]["checked"]


@pytest.mark.django_db(transaction=True)
def test_all_selected_requirements_never_mark_global_setup_complete(
    client, setup_data, tmp_path
):
    today = timezone.localdate()
    for tip, year in (("NP", 0), ("ST", today.year)):
        NumberSequence.objects.create(
            company=setup_data.company, tip=tip, leto=year, zadnja_stevilka=0
        )
    roots = [
        tmp_path.parent / f"{tmp_path.name}-{name}"
        for name in ("local", "icloud", "dropbox")
    ]
    for root in roots:
        root.mkdir()
    verify_and_save_archive_roots(*roots)
    potrdi_ddv_nastavitve(setup_data.company)
    potrdi_vzorec_podjetja(setup_data.company, setup_data.lease)
    select_chain(client, setup_data)
    response = client.get(URL + "?korak=issuer")
    assert response.context["checked_count"] == 7
    assert "Začetna nastavitev še ni zaključena" in response.content.decode()
    assert not response.context["next_step"]
    assert (
        "Neveljavno dejanje" in client.post(URL, {"action": "finish"}).content.decode()
    )
    assert not Invoice.objects.exists()
    assert not MonthlyCycle.objects.exists()
