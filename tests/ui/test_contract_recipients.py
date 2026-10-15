from datetime import date

import pytest
from django.db import connection, models
from django.test import Client
from django.urls import NoReverseMatch, reverse

from contracts.models import RecipientContact, Tenant
from contracts.services import deaktiviraj_prejemnika, ponovno_aktiviraj_prejemnika
from core.services import record_edit_fingerprint
from invoicing.models import MonthlyBatch, MonthlyCandidateReview, MonthlyCycle
from tests.financial_fixtures import save_financial_fixture
from tests.ui.test_contract_leases import make_hierarchy


def contact(lease, **changes):
    data = {
        "lease": lease,
        "email": "racuni@example.com",
        "vloga": RecipientContact.Vloga.TO,
        "kanal": RecipientContact.Kanal.PDF,
    }
    data.update(changes)
    return RecipientContact.objects.create(**data)


@pytest.mark.django_db(transaction=True)
def test_recipient_state_services_validate_and_write_inside_atomic(monkeypatch):
    recipient = contact(make_hierarchy())
    atomic_states = []
    original_clean = RecipientContact.full_clean
    original_save = models.Model.save

    def observed_clean(self, *args, **kwargs):
        atomic_states.append(("clean", connection.in_atomic_block))
        return original_clean(self, *args, **kwargs)

    def observed_save(self, *args, **kwargs):
        if kwargs.get("update_fields") == ["aktiven"]:
            atomic_states.append(("save", connection.in_atomic_block))
        return original_save(self, *args, **kwargs)

    monkeypatch.setattr(RecipientContact, "full_clean", observed_clean)
    monkeypatch.setattr(models.Model, "save", observed_save)

    assert deaktiviraj_prejemnika(recipient).uspesno
    assert ponovno_aktiviraj_prejemnika(recipient).uspesno
    assert atomic_states == [("save", True), ("clean", True), ("save", True)]


@pytest.mark.django_db
def test_lease_detail_lists_all_contacts_and_notes(client):
    lease = make_hierarchy()
    active = contact(lease, email="active@example.com")
    inactive = contact(lease, email="inactive@example.com", aktiven=False)
    page = client.get(f"/pogodbe/{lease.pk}/").content.decode()
    for text in (
        "Prejemniki računa", active.email, inactive.email,
        "TO = glavni prejemnik", "CC = kopija", "BCC = skrita kopija",
        "Računovodja podjetja se pri dostavi doda ločeno kot BCC.",
    ):
        assert text in page
    assert page.index(active.email) < page.index(inactive.email)


@pytest.mark.django_db
def test_lease_detail_recipient_empty_state(client):
    lease = make_hierarchy()
    page = client.get(f"/pogodbe/{lease.pk}/").content.decode()
    assert "Za to pogodbo še ni nastavljenih prejemnikov računa." in page
    assert "+ Dodaj prejemnika" in page


@pytest.mark.django_db
@pytest.mark.parametrize("role", list(RecipientContact.Vloga.values))
@pytest.mark.parametrize("injected_channel", ["PDF", "ESLOG", "OBOJE"])
def test_create_uses_url_parent_and_server_pdf(client, role, injected_channel):
    lease = make_hierarchy()
    other = make_hierarchy(unit=lease.unit, tenant=lease.tenant, oznaka_pogodbe="P-2")
    response = client.post(f"/pogodbe/{lease.pk}/prejemniki/nov/", {
        "email": "nov@example.com", "vloga": role,
        "kanal": injected_channel, "lease": other.pk, "aktiven": "",
    })
    created = RecipientContact.objects.get(email="nov@example.com")
    assert response.status_code == 302
    assert (created.lease, created.vloga, created.kanal, created.aktiven) == (
        lease, role, RecipientContact.Kanal.PDF, True,
    )


@pytest.mark.django_db
def test_create_page_prefills_tenant_email_and_hides_server_fields(client):
    lease = make_hierarchy()
    response = client.get(f"/pogodbe/{lease.pk}/prejemniki/nov/")
    assert response.status_code == 200
    assert response.context["form"].initial["email"] == lease.tenant.email
    assert tuple(response.context["form"].fields) == ("email", "vloga")
    page = response.content.decode()
    assert 'name="kanal"' not in page and 'name="lease"' not in page


@pytest.mark.django_db
def test_create_invalid_email_and_unusable_hierarchy(client):
    lease = make_hierarchy()
    invalid = client.post(f"/pogodbe/{lease.pk}/prejemniki/nov/", {
        "email": "ni-email", "vloga": "TO",
    })
    assert "Vnesite veljaven e-poštni naslov." in invalid.content.decode()
    lease.unit.property.company.aktivno = False
    save_financial_fixture(lease.unit.property.company, update_fields=["aktivno"])
    blocked = client.post(f"/pogodbe/{lease.pk}/prejemniki/nov/", {
        "email": "blocked@example.com", "vloga": "TO",
    })
    assert "nadrejeni zapis ni aktiven" in blocked.content.decode()
    assert not RecipientContact.objects.exists()


@pytest.mark.django_db
def test_physical_person_can_create_pdf_contact(client):
    lease = make_hierarchy()
    assert lease.tenant.tip == Tenant.Tip.FIZICNA_OSEBA
    client.post(f"/pogodbe/{lease.pk}/prejemniki/nov/", {
        "email": "oseba@example.com", "vloga": "TO",
    })
    assert RecipientContact.objects.get().kanal == RecipientContact.Kanal.PDF


@pytest.mark.django_db
def test_edit_changes_only_email_and_role_and_preserves_non_pdf(client):
    lease = make_hierarchy(tenant=Tenant.objects.create(
        tip=Tenant.Tip.PRAVNA_OSEBA, naziv="Beta", davcna_stevilka="15012557",
        naslov="B", drzava_koda="SI", identificiran_za_ddv=False,
        email="beta@example.com",
    ))
    recipient = contact(lease, kanal=RecipientContact.Kanal.ESLOG)
    other = make_hierarchy(unit=lease.unit, tenant=lease.tenant, oznaka_pogodbe="P-2")
    response = client.post(f"/pogodbe/prejemniki/{recipient.pk}/", {
        "edit_fingerprint": record_edit_fingerprint(recipient), "razlog": "Popravek",
        "email": "changed@example.com", "vloga": "BCC", "kanal": "PDF",
        "lease": other.pk, "aktiven": "",
    })
    recipient.refresh_from_db()
    assert response.status_code == 302
    assert (recipient.email, recipient.vloga, recipient.kanal, recipient.lease, recipient.aktiven) == (
        "changed@example.com", "BCC", "ESLOG", lease, True,
    )
    page = client.get(f"/pogodbe/prejemniki/{recipient.pk}/")
    assert set(page.context["form"].fields) == {"email", "vloga", "edit_fingerprint", "razlog"}
    assert "Kanal eSLOG v V1 ni urejljiv." in page.content.decode()
    assert 'name="kanal"' not in page.content.decode()


@pytest.mark.django_db
def test_state_actions_and_failed_reactivation_ignore_edit_post(client):
    lease = make_hierarchy()
    recipient = contact(lease)
    base = f"/pogodbe/prejemniki/{recipient.pk}"
    assert client.get(f"{base}/deaktiviraj/").status_code == 405
    assert client.get(f"{base}/reaktiviraj/").status_code == 405
    assert client.post(f"{base}/deaktiviraj/").status_code == 302
    recipient.refresh_from_db()
    assert recipient.aktiven is False
    lease.arhivirana = True
    save_financial_fixture(lease, update_fields=["arhivirana"])
    blocked = client.post(f"{base}/reaktiviraj/", {
        "email": "crafted@example.com", "vloga": "BCC", "kanal": "ESLOG",
    })
    recipient.refresh_from_db()
    assert blocked.status_code == 200 and blocked.context["form"].is_bound is False
    assert (recipient.email, recipient.vloga, recipient.kanal, recipient.aktiven) == (
        "racuni@example.com", "TO", "PDF", False,
    )
    lease.arhivirana = False
    save_financial_fixture(lease, update_fields=["arhivirana"])
    assert client.post(f"{base}/reaktiviraj/").status_code == 302
    recipient.refresh_from_db()
    assert recipient.aktiven is True


@pytest.mark.django_db
def test_security_and_monthly_state_isolation(client):
    lease = make_hierarchy()
    cycle = MonthlyCycle.objects.create(obdobje=date(2032, 1, 1), datum_izdaje=date(2032, 1, 5))
    batch = MonthlyBatch.objects.create(
        cycle=cycle, company=lease.unit.property.company,
        calculation_snapshot={"keep": True},
    )
    review = MonthlyCandidateReview.objects.create(
        batch=batch, lease=lease, candidate_fingerprint="a" * 64,
        selected=False, requires_confirmation=True,
    )
    client.post(f"/pogodbe/{lease.pk}/prejemniki/nov/", {
        "email": "new@example.com", "vloga": "CC",
    })
    batch.refresh_from_db()
    review.refresh_from_db()
    assert batch.calculation_snapshot == {"keep": True}
    assert (review.selected, review.requires_confirmation) == (False, True)
    assert client.get("/pogodbe/prejemniki/999999/").status_code == 404
    with pytest.raises(NoReverseMatch):
        reverse("ui:prejemnik_izbrisi", args=[1])
    csrf_client = Client(enforce_csrf_checks=True)
    recipient = RecipientContact.objects.get()
    assert csrf_client.post(
        f"/pogodbe/prejemniki/{recipient.pk}/deaktiviraj/"
    ).status_code == 403
