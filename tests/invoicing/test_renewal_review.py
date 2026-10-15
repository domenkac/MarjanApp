from copy import deepcopy
from datetime import date

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse

from contracts.models import Lease, LeaseRenewalEvent
from contracts.renewals import confirm_renewal, renewal_proposal
from invoicing.models import Invoice, MonthlyCycle
from invoicing.services import (
    izracunaj_kandidata,
    izracunaj_mesecne_kandidate,
    materializiraj_zamrznjenega_mesecnega_kandidata,
    potrdi_izjemo_kandidata,
    potrdi_osnutek_np,
    premakni_mesecni_cikel,
    preveri_predhodno_kontrolo,
    pripravi_osnutek_np,
    uskladi_preglede_mesecnega_cikla,
    zamrzni_paket_za_potrditev,
)
from tests.financial_fixtures import save_financial_fixture, update_financial_fixture
from tests.invoicing.test_preflight import (  # noqa: F401
    potrdi_septembrski_vzorec,
    ready_company,
)
from tests.setup_helpers import approve_sample, prepare_initial_setup

pytestmark = pytest.mark.django_db


def set_automatic(data, end=date(2026, 8, 31), months=12):
    lease = data["lease"]
    lease.tip_trajanja = Lease.TipTrajanja.DOLOCEN_CAS
    lease.datum_konca = end
    lease.samodejno_podaljsanje = True
    lease.obdobje_podaljsanja_mesecev = months
    save_financial_fixture(lease)
    approve_sample(data["company"], lease)
    return lease


def review_cycle(data):
    cycle = data["cycle"]
    preveri_predhodno_kontrolo(cycle)
    premakni_mesecni_cikel(cycle, MonthlyCycle.Korak.IZRACUN_OSNUTKOV)
    izracunaj_mesecne_kandidate(cycle)
    premakni_mesecni_cikel(cycle, MonthlyCycle.Korak.PAKETNI_PREGLED)
    return uskladi_preglede_mesecnega_cikla(cycle)[0]


def test_overdue_renewal_remains_billable_and_blocks_generic_confirmation(ready_company):  # noqa: F811
    data = ready_company
    lease = set_automatic(data)
    review = review_cycle(data)
    candidate = review.batch.calculation_snapshot["candidates"][0]
    assert candidate["will_issue"] and candidate["totals"]["net"] == "100.00"
    assert review.requires_confirmation and candidate["renewal"]["new_end"] == "2027-08-31"
    assert any(row["code"] == "SAMODEJNO_PODALJSANJE_OB_KONCU" for row in candidate["exceptions"])
    for action in (
        lambda: potrdi_izjemo_kandidata(data["cycle"], lease_id=lease.pk, candidate_fingerprint=review.candidate_fingerprint),
        lambda: zamrzni_paket_za_potrditev(data["cycle"]),
    ):
        with pytest.raises(ValidationError, match="[Pp]odaljšanje"):
            action()
    assert not LeaseRenewalEvent.objects.exists()


def test_old_monthly_decision_requires_refresh_after_detail_confirmation(ready_company):  # noqa: F811
    data = ready_company
    lease = set_automatic(data)
    review = review_cycle(data)
    proposal = renewal_proposal(lease, date(2026, 9, 30))
    confirm_renewal(lease.pk, proposal["source_fingerprint"], as_of=date(2026, 9, 30))
    with pytest.raises(ValidationError, match="osvežen pregled"):
        potrdi_izjemo_kandidata(data["cycle"], lease_id=lease.pk, candidate_fingerprint=review.candidate_fingerprint)
    assert not review.confirmed_at


def test_repeated_monthly_post_cannot_extend_twice(ready_company, client):  # noqa: F811
    data = ready_company
    lease = set_automatic(data)
    review = review_cycle(data)
    payload = {
        "action": "confirm_renewal", "lease_id": lease.pk,
        "candidate_fingerprint": review.candidate_fingerprint,
    }
    assert client.post(reverse("ui:mesecni_obracun"), payload).status_code == 302
    assert client.post(reverse("ui:mesecni_obracun"), payload).status_code == 200
    lease.refresh_from_db()
    assert lease.datum_konca == date(2027, 8, 31)
    assert LeaseRenewalEvent.objects.count() == 1


def test_monthly_explicit_confirmation_refreshes_review_and_issues(ready_company, client):  # noqa: F811
    data = ready_company
    lease = set_automatic(data)
    review = review_cycle(data)
    old_snapshot = deepcopy(review.batch.calculation_snapshot)
    page = client.get(reverse("ui:mesecni_obracun"))
    assert "Potrdi podaljšanje do 2027-08-31" in page.content.decode()
    response = client.post(reverse("ui:mesecni_obracun"), {
        "action": "confirm_renewal", "lease_id": lease.pk,
        "candidate_fingerprint": review.candidate_fingerprint,
    })
    assert response.status_code == 302
    lease.refresh_from_db()
    assert lease.datum_konca == date(2027, 8, 31)
    # The existing sample gate requires a new explicit sample approval.
    approve_sample(data["company"], lease)
    client.post(reverse("ui:mesecni_obracun"), {"action": "refresh_review"})
    current = uskladi_preglede_mesecnega_cikla(data["cycle"])[0]
    assert current.candidate_fingerprint != review.candidate_fingerprint
    assert current.confirmed_at is None
    assert not current.batch.calculation_snapshot["candidates"][0]["renewal"]
    assert old_snapshot["candidates"][0]["renewal"]["old_end"] == "2026-08-31"
    potrdi_izjemo_kandidata(data["cycle"], lease_id=lease.pk, candidate_fingerprint=current.candidate_fingerprint)
    zamrzni_paket_za_potrditev(data["cycle"])
    prepare_initial_setup(data["company"], lease)
    invoice, reused = materializiraj_zamrznjenega_mesecnega_kandidata(current.batch_id, lease.pk, current.candidate_fingerprint)
    assert invoice.stevilka and not reused
    assert LeaseRenewalEvent.objects.count() == 1


def test_direct_draft_issuance_requires_renewal(ready_company):  # noqa: F811
    lease = set_automatic(ready_company)
    draft = pripravi_osnutek_np(lease, date(2026, 9, 1), date(2026, 9, 30), date(2026, 9, 10)).invoice
    with pytest.raises(ValidationError, match="podaljšanje"):
        potrdi_osnutek_np(draft)
    draft.refresh_from_db()
    assert draft.stevilka is None
    proposal = renewal_proposal(lease, date(2026, 9, 30))
    confirm_renewal(lease.pk, proposal["source_fingerprint"], as_of=date(2026, 9, 30))
    prepare_initial_setup(draft.company, draft.lease)
    assert potrdi_osnutek_np(draft).stevilka


def test_renewal_preserves_frozen_snapshot_and_issued_invoice(ready_company):  # noqa: F811
    data = ready_company
    lease = set_automatic(data, date(2026, 10, 31), months=1)
    review = review_cycle(data)
    potrdi_izjemo_kandidata(data["cycle"], lease_id=lease.pk, candidate_fingerprint=review.candidate_fingerprint)
    zamrzni_paket_za_potrditev(data["cycle"])
    snapshot = deepcopy(review.batch.calculation_snapshot)
    prepare_initial_setup(data["company"], lease)
    invoice, _ = materializiraj_zamrznjenega_mesecnega_kandidata(review.batch_id, lease.pk, review.candidate_fingerprint)
    stored = Invoice.objects.filter(pk=invoice.pk).values().get()
    lines = list(invoice.invoiceline_set.values())
    proposal = renewal_proposal(lease, date(2026, 10, 31))
    confirm_renewal(lease.pk, proposal["source_fingerprint"], as_of=date(2026, 10, 31))
    review.batch.refresh_from_db()
    assert review.batch.calculation_snapshot == snapshot
    assert Invoice.objects.filter(pk=invoice.pk).values().get() == stored
    assert list(invoice.invoiceline_set.values()) == lines
    assert materializiraj_zamrznjenega_mesecnega_kandidata(review.batch_id, lease.pk, review.candidate_fingerprint)[1]


@pytest.mark.parametrize(("changes", "expected"), [
    ({"samodejno_podaljsanje": False}, "0.00"),
    ({"arhivirana": True}, "0.00"),
    ({"status": Lease.Status.PRENEHALA, "datum_prenehanja": date(2026, 8, 31)}, "0.00"),
    ({"status": Lease.Status.ODPOVEDANA, "datum_odpovedi": date(2026, 8, 1), "datum_prenehanja": date(2026, 9, 15)}, "61.00"),
    ({"datum_prenehanja": date(2026, 9, 15)}, "61.00"),
    ({"datum_odpovedi": date(2026, 8, 1)}, "0.00"),
])
def test_actual_termination_archive_and_nonrenewing_terms(ready_company, changes, expected):  # noqa: F811
    lease = set_automatic(ready_company)
    update_financial_fixture(Lease.objects.filter(pk=lease.pk), **changes)
    lease.refresh_from_db()
    assert renewal_proposal(lease, date(2026, 9, 30)) is None
    candidate = izracunaj_kandidata(lease, date(2026, 9, 1), date(2026, 9, 30), date(2026, 9, 10))
    assert str(candidate.skupaj_z_ddv) == expected


def test_expiry_inside_month_keeps_full_rent_pending_confirmation(ready_company):  # noqa: F811
    lease = set_automatic(ready_company, date(2026, 9, 15))
    review = review_cycle(ready_company)
    candidate = review.batch.calculation_snapshot["candidates"][0]
    assert candidate["totals"]["net"] == "100.00"
    assert candidate["renewal"]["old_end"] == "2026-09-15"
    lease.refresh_from_db()
    assert lease.datum_konca == date(2026, 9, 15)


def test_pending_renewal_on_system_nonissue_row_is_visible(ready_company, client):  # noqa: F811
    data = ready_company
    pending = Lease.objects.create(
        unit=data["unit"], tenant=data["tenant"], oznaka_pogodbe="P-PENDING",
        datum_zacetka=date(2026, 1, 1), datum_konca=date(2026, 8, 31),
        tip_trajanja=Lease.TipTrajanja.DOLOCEN_CAS, samodejno_podaljsanje=True,
        obdobje_podaljsanja_mesecev=12, rok_placila_dni=15,
    )
    review = review_cycle(data)
    candidate = next(row for row in review.batch.calculation_snapshot["candidates"] if row["lease_id"] == pending.pk)
    assert candidate["will_issue"] is False and candidate["renewal"]
    page = client.get(reverse("ui:mesecni_obracun")).content.decode()
    assert "Potrdi podaljšanje do 2027-08-31" in page


def test_frozen_issuance_rechecks_pending_renewal(ready_company):  # noqa: F811
    data = ready_company
    lease = data["lease"]
    review = review_cycle(data)
    potrdi_izjemo_kandidata(data["cycle"], lease_id=lease.pk, candidate_fingerprint=review.candidate_fingerprint)
    zamrzni_paket_za_potrditev(data["cycle"])
    set_automatic(data)
    snapshot = deepcopy(review.batch.calculation_snapshot)
    with pytest.raises(ValidationError, match="podaljšanje"):
        materializiraj_zamrznjenega_mesecnega_kandidata(review.batch_id, lease.pk, review.candidate_fingerprint)
    assert not Invoice.objects.exists()
    review.batch.refresh_from_db()
    assert review.batch.calculation_snapshot == snapshot
