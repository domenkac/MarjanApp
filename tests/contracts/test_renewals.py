from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Barrier

import pytest
from django.core.exceptions import ValidationError
from django.db import close_old_connections, transaction
from django.db.models.deletion import ProtectedError

from contracts.models import Lease, LeaseRenewalEvent
from contracts.renewals import (
    add_calendar_months,
    confirm_renewal,
    renewal_proposal,
)
from contracts.services import efektivna_veljavnost_pogodbe
from core.models import AuditLogEntry
from tests.financial_fixtures import update_financial_fixture
from tests.ui.test_contract_leases import make_hierarchy


def automatic_lease(**changes):
    return make_hierarchy(**{
        "tip_trajanja": Lease.TipTrajanja.DOLOCEN_CAS,
        "datum_zacetka": date(2023, 1, 1), "datum_konca": date(2024, 1, 31),
        "samodejno_podaljsanje": True, "obdobje_podaljsanja_mesecev": 1,
        **changes,
    })


@pytest.mark.parametrize(("end", "months", "expected"), [
    (date(2024, 1, 31), 1, date(2024, 2, 29)),
    (date(2025, 1, 31), 1, date(2025, 2, 28)),
    (date(2024, 2, 29), 12, date(2025, 2, 28)),
    (date(2025, 12, 31), 2, date(2026, 2, 28)),
    (date(2026, 4, 30), 1, date(2026, 5, 30)),
])
def test_calendar_months(end, months, expected):
    assert add_calendar_months(end, months) == expected


@pytest.mark.parametrize("months", [None, 0, -1, True, "12", 120000])
def test_invalid_renewal_period(months):
    with pytest.raises(ValidationError):
        add_calendar_months(date(2024, 1, 31), months)


@pytest.mark.django_db
def test_expiry_detection_is_read_only_and_overdue_requires_each_confirmation():
    lease = automatic_lease()
    assert renewal_proposal(lease, date(2024, 1, 30)) is None
    first = renewal_proposal(lease, date(2024, 1, 31))
    assert first["new_end"] == "2024-02-29"
    assert renewal_proposal(lease, date(2026, 9, 30)) == first
    assert efektivna_veljavnost_pogodbe(lease)[1] is None
    lease.refresh_from_db()
    assert lease.datum_konca == date(2024, 1, 31)
    assert not LeaseRenewalEvent.objects.exists()
    event = confirm_renewal(lease.pk, first["source_fingerprint"], as_of=date(2026, 9, 30))
    lease.refresh_from_db()
    assert event.new_end == lease.datum_konca == date(2024, 2, 29)
    assert renewal_proposal(lease, date(2026, 9, 30))["old_end"] == "2024-02-29"
    assert LeaseRenewalEvent.objects.count() == 1


@pytest.mark.django_db
def test_confirmation_audit_and_retry_are_atomic_and_immutable(monkeypatch):
    lease = automatic_lease()
    proposal = renewal_proposal(lease, date(2024, 1, 31))
    event = confirm_renewal(lease.pk, proposal["source_fingerprint"], as_of=date(2024, 1, 31))
    audit = event.audit
    assert event.decision == LeaseRenewalEvent.Decision.CONFIRMED and event.decided_at
    assert audit.object_id == lease.pk and audit.polje == "datum_konca"
    assert audit.stara_vrednost == "2024-01-31" and audit.nova_vrednost == "2024-02-29"
    assert confirm_renewal(lease.pk, proposal["source_fingerprint"], as_of=date(2026, 1, 1)).pk == event.pk
    assert LeaseRenewalEvent.objects.count() == AuditLogEntry.objects.count() == 1
    for mutation in (
        lambda: event.save(), lambda: event.delete(),
        lambda: LeaseRenewalEvent.objects.update(new_end=date(2027, 1, 1)),
        lambda: LeaseRenewalEvent.objects.all().delete(),
        lambda: LeaseRenewalEvent.objects.bulk_update([event], ["new_end"]),
        lambda: LeaseRenewalEvent.objects.bulk_create([event], update_conflicts=True),
    ):
        with pytest.raises(ValidationError):
            mutation()
    with pytest.raises(ProtectedError):
        lease.delete()
    with pytest.raises(ValidationError, match="Revizijske"):
        audit.delete()


@pytest.mark.django_db
@pytest.mark.parametrize("failure", ["audit", "event"])
def test_confirmation_rolls_back_on_evidence_failure(monkeypatch, failure):
    lease = automatic_lease()
    proposal = renewal_proposal(lease, date(2024, 1, 31))
    manager = AuditLogEntry.objects if failure == "audit" else LeaseRenewalEvent.objects
    original = manager.create

    def fail(**kwargs):
        raise RuntimeError("Evidence unavailable")

    monkeypatch.setattr(manager, "create", fail)
    with pytest.raises(RuntimeError, match="Evidence unavailable"):
        confirm_renewal(lease.pk, proposal["source_fingerprint"], as_of=date(2024, 1, 31))
    lease.refresh_from_db()
    assert lease.datum_konca == date(2024, 1, 31)
    assert not LeaseRenewalEvent.objects.exists() and not AuditLogEntry.objects.exists()
    monkeypatch.setattr(manager, "create", original)
    confirm_renewal(lease.pk, proposal["source_fingerprint"], as_of=date(2024, 1, 31))


@pytest.mark.django_db
@pytest.mark.parametrize("changes", [
    {"arhivirana": True}, {"samodejno_podaljsanje": False},
    {"status": Lease.Status.ODPOVEDANA, "datum_odpovedi": date(2024, 1, 1), "datum_prenehanja": date(2024, 2, 15)},
    {"status": Lease.Status.PRENEHALA, "datum_prenehanja": date(2024, 1, 15)},
    {"datum_odpovedi": date(2024, 1, 1)}, {"datum_prenehanja": date(2024, 2, 15)},
    {"obdobje_podaljsanja_mesecev": 12}, {"datum_konca": date(2024, 2, 29)},
])
def test_stale_decisions_cannot_extend_changed_lease(changes):
    lease = automatic_lease()
    proposal = renewal_proposal(lease, date(2024, 1, 31))
    update_financial_fixture(Lease.objects.filter(pk=lease.pk), **changes)
    with pytest.raises(ValidationError):
        confirm_renewal(lease.pk, proposal["source_fingerprint"], as_of=date(2024, 3, 31))
    assert not LeaseRenewalEvent.objects.exists()


@pytest.mark.django_db
def test_termination_nonrenewing_and_archived_leases():
    lease = automatic_lease(datum_prenehanja=date(2024, 2, 15))
    assert renewal_proposal(lease, date(2024, 3, 31)) is None
    assert efektivna_veljavnost_pogodbe(lease)[1] == date(2024, 2, 15)
    lease.datum_prenehanja = None
    lease.samodejno_podaljsanje = False
    assert efektivna_veljavnost_pogodbe(lease)[1] == date(2024, 1, 31)
    assert renewal_proposal(lease, date(2024, 3, 31)) is None
    lease.samodejno_podaljsanje = True
    lease.unit.aktivna = False
    assert renewal_proposal(lease, date(2024, 3, 31)) is None


@pytest.mark.django_db(transaction=True)
def test_concurrent_confirmations_create_one_event_and_audit():
    lease = automatic_lease()
    fingerprint = renewal_proposal(lease, date(2024, 1, 31))["source_fingerprint"]
    barrier = Barrier(2)

    def confirm():
        close_old_connections()
        barrier.wait(timeout=10)
        try:
            return confirm_renewal(lease.pk, fingerprint, as_of=date(2024, 1, 31)).pk
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(confirm) for _ in range(2)]
        assert futures[0].result(timeout=20) == futures[1].result(timeout=20)
    assert LeaseRenewalEvent.objects.count() == AuditLogEntry.objects.count() == 1
    lease.refresh_from_db()
    assert lease.datum_konca == date(2024, 2, 29)


@pytest.mark.django_db
def test_duplicate_old_end_is_rejected_after_manual_date_reversal():
    lease = automatic_lease()
    proposal = renewal_proposal(lease, date(2024, 1, 31))
    confirm_renewal(lease.pk, proposal["source_fingerprint"], as_of=date(2024, 1, 31))
    update_financial_fixture(Lease.objects.filter(pk=lease.pk), datum_konca=date(2024, 1, 31), rok_placila_dni=20)
    lease.refresh_from_db()
    proposal = renewal_proposal(lease, date(2024, 1, 31))
    with pytest.raises(ValidationError), transaction.atomic():
        confirm_renewal(lease.pk, proposal["source_fingerprint"], as_of=date(2024, 1, 31))
