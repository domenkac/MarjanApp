from datetime import date

import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse

from contracts.models import Lease, LeaseRenewalEvent
from contracts.renewals import (
    confirm_renewal,
    lease_source_fingerprint,
    renewal_proposal,
    save_lease_edit,
)
from core.models import AuditLogEntry
from tests.contracts.test_renewals import automatic_lease
from tests.ui.test_contract_leases import lease_data
from ui.forms import LeaseForm

pytestmark = pytest.mark.django_db


def edit_data(lease, **changes):
    data = lease_data(lease, **{
        "tip_trajanja": lease.tip_trajanja,
        "datum_konca": lease.datum_konca.isoformat() if lease.datum_konca else "",
        "samodejno_podaljsanje": "on" if lease.samodejno_podaljsanje else "",
        "obdobje_podaljsanja_mesecev": lease.obdobje_podaljsanja_mesecev or "",
        **changes,
    })
    data["edit_fingerprint"] = LeaseForm(instance=lease).initial.get("edit_fingerprint", "")
    return data


@pytest.mark.parametrize("changes", [
    {"datum_konca": "2025-01-31"},
    {"datum_konca": "2025-01-31", "samodejno_podaljsanje": ""},
    {"tip_trajanja": Lease.TipTrajanja.NEDOLOCEN_CAS, "datum_konca": "", "samodejno_podaljsanje": ""},
    {"samodejno_podaljsanje": ""},
])
def test_ordinary_form_cannot_confirm_expired_renewal(client, monkeypatch, changes):
    monkeypatch.setattr("ui.views.timezone.localdate", lambda: date(2024, 1, 31))
    lease = automatic_lease()
    data = edit_data(lease, **changes)
    form = LeaseForm(data, instance=lease)
    assert not form.is_valid()
    assert "Potrdi podaljšanje" in str(form.errors)
    response = client.post(reverse("ui:pogodba_detail", args=[lease.pk]), data)
    assert response.status_code == 200 and "Potrdi podaljšanje" in str(response.context["form"].errors)
    lease.refresh_from_db()
    assert lease.datum_konca == date(2024, 1, 31)
    assert lease.samodejno_podaljsanje and lease.tip_trajanja == Lease.TipTrajanja.DOLOCEN_CAS
    assert not LeaseRenewalEvent.objects.exists() and not AuditLogEntry.objects.exists()


def test_model_save_cannot_bypass_renewal_service(monkeypatch):
    monkeypatch.setattr("ui.views.timezone.localdate", lambda: date(2024, 1, 31))
    lease = automatic_lease()
    lease.datum_konca = date(2025, 1, 31)
    with pytest.raises(ValidationError, match="Potrdi podaljšanje"):
        lease.save(update_fields=["datum_konca"])
    lease.refresh_from_db()
    assert lease.datum_konca == date(2024, 1, 31)
    proposal = renewal_proposal(lease, date(2024, 1, 31))
    event = confirm_renewal(lease.pk, proposal["source_fingerprint"], as_of=date(2024, 1, 31))
    assert confirm_renewal(lease.pk, proposal["source_fingerprint"], as_of=date(2024, 1, 31)).pk == event.pk
    assert LeaseRenewalEvent.objects.count() == AuditLogEntry.objects.count() == 1


def test_preexpiry_edits_and_expired_termination_remain_permitted(client, monkeypatch):
    monkeypatch.setattr("ui.views.timezone.localdate", lambda: date(2024, 1, 30))
    lease = automatic_lease()
    data = edit_data(lease, datum_konca="2024-02-29", rok_placila_dni="21")
    assert client.post(reverse("ui:pogodba_detail", args=[lease.pk]), data).status_code == 302
    lease.refresh_from_db()
    assert lease.datum_konca == date(2024, 2, 29) and lease.rok_placila_dni == 21
    monkeypatch.setattr("ui.views.timezone.localdate", lambda: date(2024, 3, 1))
    data = edit_data(lease, status=Lease.Status.ODPOVEDANA, datum_odpovedi="2024-02-15", datum_prenehanja="2024-03-15")
    assert client.post(reverse("ui:pogodba_detail", args=[lease.pk]), data).status_code == 302
    lease.refresh_from_db()
    assert lease.datum_prenehanja == date(2024, 3, 15) and lease.datum_konca == date(2024, 2, 29)
    assert not LeaseRenewalEvent.objects.exists()


def test_stale_form_cannot_overwrite_confirmed_renewal(client, monkeypatch):
    monkeypatch.setattr("ui.views.timezone.localdate", lambda: date(2024, 1, 31))
    lease = automatic_lease()
    data = edit_data(lease, rok_placila_dni="21")
    proposal = renewal_proposal(lease, date(2024, 1, 31))
    confirm_renewal(lease.pk, proposal["source_fingerprint"], as_of=date(2024, 1, 31))
    response = client.post(reverse("ui:pogodba_detail", args=[lease.pk]), data)
    assert response.status_code == 200 and "Osvežite" in str(response.context["form"].errors)
    lease.refresh_from_db()
    assert lease.datum_konca == date(2024, 2, 29) and lease.rok_placila_dni == 15
    assert LeaseRenewalEvent.objects.count() == 1


def test_form_save_rechecks_after_validation(monkeypatch):
    monkeypatch.setattr("ui.views.timezone.localdate", lambda: date(2024, 1, 31))
    lease = automatic_lease()
    form = LeaseForm(edit_data(lease, rok_placila_dni="21"), instance=lease)
    assert form.is_valid()
    fresh = type(lease).objects.get(pk=lease.pk)
    proposal = renewal_proposal(fresh, date(2024, 1, 31))
    confirm_renewal(lease.pk, proposal["source_fingerprint"], as_of=date(2024, 1, 31))
    with pytest.raises(ValidationError, match="Osvežite"):
        form.save()
    lease.refresh_from_db()
    assert lease.datum_konca == date(2024, 2, 29) and lease.rok_placila_dni == 15


def test_edit_service_and_deferred_form_save_cannot_bypass_confirmation(monkeypatch):
    monkeypatch.setattr("ui.views.timezone.localdate", lambda: date(2024, 1, 31))
    lease = automatic_lease()
    fingerprint = lease_source_fingerprint(lease)
    lease.datum_konca = date(2025, 1, 31)
    with pytest.raises(ValidationError, match="Potrdi podaljšanje"):
        save_lease_edit(lease, source_fingerprint=fingerprint)
    lease.refresh_from_db()
    form = LeaseForm(edit_data(lease, rok_placila_dni="21"), instance=lease)
    assert form.is_valid()
    deferred = form.save(commit=False)
    fresh = Lease.objects.get(pk=lease.pk)
    proposal = renewal_proposal(fresh, date(2024, 1, 31))
    confirm_renewal(lease.pk, proposal["source_fingerprint"], as_of=date(2024, 1, 31))
    with pytest.raises(ValidationError, match="Osvežite"):
        deferred.save()
    lease.refresh_from_db()
    assert lease.datum_konca == date(2024, 2, 29) and lease.rok_placila_dni == 15


def test_edit_validation_rechecks_expiry_at_save(monkeypatch):
    monkeypatch.setattr("ui.views.timezone.localdate", lambda: date(2024, 1, 30))
    lease = automatic_lease()
    form = LeaseForm(edit_data(lease, datum_konca="2025-01-31"), instance=lease)
    assert form.is_valid()
    monkeypatch.setattr("ui.views.timezone.localdate", lambda: date(2024, 1, 31))
    with pytest.raises(ValidationError, match="Potrdi podaljšanje"):
        form.save()
    lease.refresh_from_db()
    assert lease.datum_konca == date(2024, 1, 31)
    assert not LeaseRenewalEvent.objects.exists() and not AuditLogEntry.objects.exists()


def test_expired_metadata_edit_and_missing_version(client, monkeypatch):
    monkeypatch.setattr("ui.views.timezone.localdate", lambda: date(2024, 1, 31))
    lease = automatic_lease()
    data = edit_data(lease, rok_placila_dni="21")
    data.pop("edit_fingerprint")
    url = reverse("ui:pogodba_detail", args=[lease.pk])
    assert client.post(url, data).status_code == 200
    lease.refresh_from_db()
    assert lease.rok_placila_dni == 15
    assert client.post(url, edit_data(lease, rok_placila_dni="21")).status_code == 302
    lease.refresh_from_db()
    assert lease.rok_placila_dni == 21 and lease.datum_konca == date(2024, 1, 31)
    assert not LeaseRenewalEvent.objects.exists()


def test_detail_requires_explicit_post_and_shows_history(client, monkeypatch):
    monkeypatch.setattr("ui.views.timezone.localdate", lambda: date(2024, 1, 31))
    lease = automatic_lease()
    url = reverse("ui:pogodba_podaljsaj", args=[lease.pk])
    detail = reverse("ui:pogodba_detail", args=[lease.pk])
    page = client.get(detail)
    assert "Obvezen pregled podaljšanja" in page.content.decode()
    assert "2024-02-29" in page.content.decode()
    assert not LeaseRenewalEvent.objects.exists()
    assert client.get(url).status_code == 405
    assert Client(enforce_csrf_checks=True).post(url).status_code == 403
    assert client.post(url).status_code == 200
    assert not LeaseRenewalEvent.objects.exists()
    proposal = renewal_proposal(lease, date(2024, 1, 31))
    payload = {"source_fingerprint": proposal["source_fingerprint"]}
    assert client.post(url, payload).status_code == 302
    assert client.post(url, payload).status_code == 302
    lease.refresh_from_db()
    assert lease.datum_konca == date(2024, 2, 29)
    assert LeaseRenewalEvent.objects.count() == AuditLogEntry.objects.count() == 1
    page = client.get(detail).content.decode()
    assert "Zgodovina podaljšanj" in page and "Potrjeno" in page
    assert "Obvezen pregled podaljšanja" not in page


@pytest.mark.parametrize("changes", [
    {"arhivirana": True}, {"datum_odpovedi": date(2024, 1, 15)},
    {"status": Lease.Status.ODPOVEDANA, "datum_prenehanja": date(2024, 2, 15)},
    {"obdobje_podaljsanja_mesecev": 12},
])
def test_stale_detail_post_requires_new_review(client, monkeypatch, changes):
    monkeypatch.setattr("ui.views.timezone.localdate", lambda: date(2024, 1, 31))
    lease = automatic_lease()
    proposal = renewal_proposal(lease, date(2024, 1, 31))
    Lease.objects.filter(pk=lease.pk).update(**changes)
    response = client.post(reverse("ui:pogodba_podaljsaj", args=[lease.pk]), {
        "source_fingerprint": proposal["source_fingerprint"],
    })
    assert response.status_code == 200
    assert "ni več veljaven" in response.content.decode()
    assert not LeaseRenewalEvent.objects.exists()
    lease.refresh_from_db()
    assert lease.datum_konca == date(2024, 1, 31)


def test_overdue_detail_never_hides_remaining_unconfirmed_periods(client, monkeypatch):
    monkeypatch.setattr("ui.views.timezone.localdate", lambda: date(2026, 9, 30))
    lease = automatic_lease()
    proposal = renewal_proposal(lease, date(2026, 9, 30))
    client.post(reverse("ui:pogodba_podaljsaj", args=[lease.pk]), {
        "source_fingerprint": proposal["source_fingerprint"],
    })
    page = client.get(reverse("ui:pogodba_detail", args=[lease.pk])).content.decode()
    assert "Obvezen pregled podaljšanja" in page
    assert "2024-03-29" in page
    assert LeaseRenewalEvent.objects.count() == 1
