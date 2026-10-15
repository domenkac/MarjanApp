import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from threading import Barrier

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import close_old_connections
from django.test import Client
from django.urls import reverse

from companies.models import Company
from contracts.indexation import (
    batch_is_stale,
    confirm_batch,
    create_batch,
    save_review,
    validate_input,
)
from contracts.models import (
    IndexationBatch,
    IndexationEvent,
    Lease,
    LeaseItem,
    Property,
    Unit,
)
from contracts.services import izracunaj_postavko
from core.models import AuditLogEntry
from invoicing.models import Invoice, InvoiceLine, MonthlyBatch, MonthlyCycle
from tests.contracts.test_indeksacija import postavka, racun_s_postavkami  # noqa: F401
from tests.financial_fixtures import save_financial_fixture, update_financial_fixture


def batch_for(item, value="110.000"):
    return create_batch(item.lease.unit.property.company_id, 2025, Decimal(value))


@pytest.mark.parametrize(
    "year,value",
    [
        (2025, Decimal(0)),
        (2025, Decimal(-1)),
        (2025, Decimal("NaN")),
        (2025, Decimal(1000)),
        (2025, Decimal("100.0001")),
        (2025, 100.0),
        (True, Decimal(100)),
        (9999, Decimal(100)),
    ],
)
def test_invalid_input(year, value):
    with pytest.raises(ValidationError):
        validate_input(year, value)


def test_year_effective_date_and_exact_audit(postavka):  # noqa: F811
    batch = batch_for(postavka)
    assert batch.effective_date == date(2026, 2, 1)
    row = batch.decisions.get()
    assert row.proposal["old_rent"] == "100.05"
    assert row.proposal["new_rent"] == "110.06"
    confirm_batch(batch.company_id, batch.pk, batch.revision)
    row.refresh_from_db()
    assert row.event.datum_uskladitve == date(2026, 2, 1)
    assert row.event.nova_vrednost == Decimal("110.06")
    audit = AuditLogEntry.objects.get(
        content_type=ContentType.objects.get_for_model(row)
    )
    assert (audit.polje, audit.stara_vrednost, audit.nova_vrednost) == (
        "outcome",
        "PENDING",
        "APPROVED",
    )
    payload = json.loads(audit.razlog)
    assert payload["index_year"] == 2025
    assert payload["proposal"] == row.proposal
    assert audit.created_at and row.decided_at
    assert not AuditLogEntry.objects.filter(
        content_type=ContentType.objects.get_for_model(LeaseItem)
    ).exists()
    postavka.refresh_from_db()
    assert postavka.znesek_neto_mesecno == Decimal("100.05")


@pytest.mark.parametrize(
    "condition,value,threshold,clause,text",
    [
        ("PRAG_SPREMEMBE", "101.000", "2.00", True, "pod pragom"),
        ("SAMO_NAVZGOR", "99.000", None, True, "Indeks padel"),
        ("", "110.000", None, False, "nima klavzule"),
    ],
)
def test_automatic_skipped_decisions(
    postavka,  # noqa: F811
    condition,
    value,
    threshold,
    clause,
    text,
):
    update_financial_fixture(Lease.objects.filter(pk=postavka.lease_id),
        ima_klavzulo_indeksacije=clause,
        tip_pogoja_indeksacije=condition,
        prag_spremembe_odstotek=Decimal(threshold) if threshold else None,
    )
    batch = batch_for(postavka, value)
    row = batch.decisions.get()
    assert not row.selected and not row.proposal["blocked"]
    assert text in row.proposal["explanation"]
    if clause:
        assert row.proposal["new_rent"] and row.proposal["percentage"]
    confirm_batch(batch.company_id, batch.pk, 1)
    row.refresh_from_db()
    assert row.outcome == "SKIPPED" and text in row.reason
    assert not IndexationEvent.objects.exists()


def test_multiple_items_excluded_at_contract_level(postavka):  # noqa: F811
    other = LeaseItem.objects.create(
        lease=postavka.lease,
        naziv_postavke="Parkiranje",
        znesek_neto_mesecno=Decimal(50),
        vat_regime=postavka.vat_regime,
        indeks_ob_osnovi=Decimal(100),
        datum_osnove_indeksacije=date(2025, 1, 1),
    )
    batch = batch_for(postavka)
    assert batch.decisions.count() == 2
    with pytest.raises(ValidationError, match="obvezen"):
        save_review(batch.company_id, batch.pk, 1, set(), {postavka.lease_id: " "})
    batch = save_review(
        batch.company_id,
        batch.pk,
        1,
        set(batch.decisions.values_list("pk", flat=True)),
        {other.lease_id: "Dogovor z najemnikom"},
    )
    confirm_batch(batch.company_id, batch.pk, batch.revision)
    assert set(batch.decisions.values_list("outcome", flat=True)) == {"EXCLUDED"}
    assert set(batch.decisions.values_list("reason", flat=True)) == {
        "Dogovor z najemnikom"
    }
    assert not IndexationEvent.objects.exists()


def test_eligible_deselection_remains_pending_until_justified_exclusion(postavka):  # noqa: F811
    batch = batch_for(postavka)
    batch = save_review(batch.company_id, batch.pk, 1, set(), {})
    with pytest.raises(ValidationError, match="izključitev pogodbe z razlogom"):
        confirm_batch(batch.company_id, batch.pk, batch.revision)
    row = batch.decisions.get()
    assert row.outcome == "PENDING" and row.exclusion_reason == "" and not row.selected
    assert not IndexationEvent.objects.exists()
    assert not AuditLogEntry.objects.exists()
    batch = save_review(batch.company_id, batch.pk, batch.revision, set(), {postavka.lease_id: "Dogovor"})
    confirm_batch(batch.company_id, batch.pk, batch.revision)
    assert batch.decisions.get().outcome == "EXCLUDED"


@pytest.mark.parametrize(
    "change", ["rent", "tenant", "clause", "new_item", "invoice", "vat", "history", "freeze"]
)
def test_source_changes_invalidate_preview(postavka, change):  # noqa: F811
    batch = batch_for(postavka)
    if change == "rent":
        update_financial_fixture(LeaseItem.objects.filter(pk=postavka.pk),
            znesek_neto_mesecno=Decimal(120)
        )
    elif change == "tenant":
        tenant = postavka.lease.tenant
        tenant.naziv = "Spremenjen najemnik"
        tenant.save()
    elif change == "clause":
        update_financial_fixture(Lease.objects.filter(pk=postavka.lease_id),
            ima_klavzulo_indeksacije=False
        )
    elif change == "new_item":
        LeaseItem.objects.create(
            lease=postavka.lease,
            vat_regime=postavka.vat_regime,
            znesek_neto_mesecno=Decimal(20),
        )
    elif change == "invoice":
        racun_s_postavkami(postavka)
    elif change == "vat":
        regime = postavka.vat_regime
        regime.stopnja = Decimal("9.50")
        save_financial_fixture(regime)
    elif change == "freeze":
        cycle = MonthlyCycle.objects.create(
            obdobje=date(2026, 2, 1), datum_izdaje=date(2026, 2, 1),
            trenutni_korak=MonthlyCycle.Korak.POTRDITEV_PAKETA,
        )
        MonthlyBatch.objects.create(
            cycle=cycle, company_id=batch.company_id,
            calculation_snapshot={"candidates": [{"lease_id": postavka.lease_id}]},
        )
    else:
        AuditLogEntry.objects.create(
            content_type=ContentType.objects.get_for_model(postavka),
            object_id=postavka.pk,
            polje="znesek_neto_mesecno",
            stara_vrednost="90",
            nova_vrednost="100.05",
            razlog="Popravek",
        )
    assert batch_is_stale(batch)
    with pytest.raises(ValidationError, match="zastarel"):
        confirm_batch(batch.company_id, batch.pk, 1)
    assert not IndexationEvent.objects.exists()


def test_five_company_isolation(postavka):  # noqa: F811
    original_company = postavka.lease.unit.property.company
    companies = [original_company]
    for number in range(4):
        company = Company.objects.get(pk=original_company.pk)
        company.pk = None
        company.naziv = f"Podjetje {number}"
        company.davcna_stevilka = f"1501255{number}"
        save_financial_fixture(company)
        companies.append(company)
        prop = Property.objects.create(
            company=company, naziv="Stavba", naslov="Ulica 1"
        )
        unit = Unit.objects.create(property=prop, oznaka="Enota", tip_enote="PISARNA")
        lease = Lease.objects.get(pk=postavka.lease_id)
        lease.pk = None
        lease.unit = unit
        save_financial_fixture(lease)
        item = LeaseItem.objects.get(pk=postavka.pk)
        item.pk = None
        item.lease = lease
        save_financial_fixture(item)
    batches = [create_batch(company.pk, 2025, Decimal(110 + number)) for number, company in enumerate(companies)]
    assert all(batch.decisions.count() == 1 for batch in batches)
    with pytest.raises(IndexationBatch.DoesNotExist):
        confirm_batch(companies[1].pk, batches[0].pk, 1)
    with pytest.raises(ValidationError):
        save_review(
            batches[0].company_id, batches[0].pk, 1, {batches[1].decisions.get().pk}, {}
        )
    confirm_batch(companies[0].pk, batches[0].pk, 1)
    assert IndexationEvent.objects.count() == 1
    assert all(not batch_is_stale(batch) for batch in batches[1:])
    for batch in batches[1:]:
        confirm_batch(batch.company_id, batch.pk, 1)
        assert batch.decisions.get().event.indeks_vrednost == batch.index_value
    assert IndexationEvent.objects.count() == 5


def test_duplicate_submission_and_revision(postavka):  # noqa: F811
    batch = batch_for(postavka)
    competing = batch_for(postavka, "111.000")
    batch = save_review(
        batch.company_id,
        batch.pk,
        1,
        set(batch.decisions.values_list("pk", flat=True)),
        {},
    )
    with pytest.raises(ValidationError, match="spremenjen"):
        confirm_batch(batch.company_id, batch.pk, 1)
    confirm_batch(batch.company_id, batch.pk, batch.revision)
    audit_count = AuditLogEntry.objects.count()
    confirm_batch(batch.company_id, batch.pk, batch.revision)
    assert (
        IndexationEvent.objects.count() == 1
        and AuditLogEntry.objects.count() == audit_count
    )
    with pytest.raises(ValidationError, match="objavljenega indeksa"):
        confirm_batch(competing.company_id, competing.pk, 1)


@pytest.mark.parametrize("failure_number", [1, 2, 3, 4])
def test_audit_failure_rolls_back_entire_batch(postavka, monkeypatch, failure_number):  # noqa: F811
    LeaseItem.objects.create(
        lease=postavka.lease,
        vat_regime=postavka.vat_regime,
        znesek_neto_mesecno=Decimal(20),
        indeks_ob_osnovi=Decimal(100),
        datum_osnove_indeksacije=date(2025, 1, 1),
    )
    batch = batch_for(postavka)
    original = AuditLogEntry.save
    count = 0

    def fail(self, *args, **kwargs):
        nonlocal count
        count += 1
        if count == failure_number:
            raise RuntimeError("audit failure")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(AuditLogEntry, "save", fail)
    with pytest.raises(RuntimeError, match="audit failure"):
        confirm_batch(batch.company_id, batch.pk, 1)
    assert not IndexationEvent.objects.exists() and not AuditLogEntry.objects.exists()
    assert set(batch.decisions.values_list("outcome", flat=True)) == {"PENDING"}
    batch.refresh_from_db()
    assert batch.state == "PENDING" and batch.revision == 1


def test_missing_historical_rent_is_visible_and_blocked(postavka):  # noqa: F811
    update_financial_fixture(LeaseItem.objects.filter(pk=postavka.pk),
        referencna_najemnina=None, datum_referencne_najemnine=None
    )
    batch = batch_for(postavka)
    row = batch.decisions.get()
    assert row.proposal["blocked"] and "nerazjasnjena" in row.proposal["explanation"]
    with pytest.raises(ValidationError, match="Blokirana"):
        confirm_batch(batch.company_id, batch.pk, 1)
    assert not IndexationEvent.objects.exists()


def test_issued_invoice_is_immutable_and_late_case_blocked(postavka):  # noqa: F811
    invoice = racun_s_postavkami(postavka)
    before = (list(Invoice.objects.values()), list(InvoiceLine.objects.values()))
    batch = batch_for(postavka)
    assert "T2-C" in batch.decisions.get().proposal["explanation"]
    with pytest.raises(ValidationError):
        confirm_batch(batch.company_id, batch.pk, 1)
    invoice.refresh_from_db()
    assert (
        list(Invoice.objects.values()),
        list(InvoiceLine.objects.values()),
    ) == before
    assert not IndexationEvent.objects.exists()


def test_frozen_package_is_immutable_and_blocks_confirmation(postavka):  # noqa: F811
    cycle = MonthlyCycle.objects.create(
        obdobje=date(2026, 2, 1),
        datum_izdaje=date(2026, 2, 1),
        trenutni_korak=MonthlyCycle.Korak.POTRDITEV_PAKETA,
    )
    monthly = MonthlyBatch.objects.create(
        cycle=cycle,
        company=postavka.lease.unit.property.company,
        calculation_snapshot={
            "candidates": [
                {
                    "lease_id": postavka.lease_id,
                    "lines": [{"lease_item_id": postavka.pk, "net": "100.05"}],
                }
            ]
        },
    )
    before = MonthlyBatch.objects.values().get(pk=monthly.pk)
    batch = batch_for(postavka)
    assert "zamrznjenem" in batch.decisions.get().proposal["explanation"]
    with pytest.raises(ValidationError):
        confirm_batch(batch.company_id, batch.pk, 1)
    assert MonthlyBatch.objects.values().get(pk=monthly.pk) == before


def test_browser_restart_resume_and_company_route(postavka):  # noqa: F811
    company_id = postavka.lease.unit.property.company_id
    response = Client().post(
        reverse("ui:indexation_list"),
        {"company": company_id, "index_year": "2025", "index_value": "110.000"},
    )
    assert response.status_code == 302
    batch = IndexationBatch.objects.get()
    url = response.url
    response = Client().post(
        url,
        {
            "action": "save",
            "revision": 1,
            f"exclude_{postavka.lease_id}": "on",
            f"reason_{postavka.lease_id}": "Dogovor <script>",
        },
    )
    assert response.status_code == 302
    restarted = Client()
    response = restarted.get(url)
    assert response.status_code == 200 and b"Dogovor &lt;script&gt;" in response.content
    assert (
        restarted.get(
            reverse("ui:indexation_detail", args=[company_id + 99, batch.pk])
        ).status_code
        == 404
    )
    response = restarted.post(url, {"action": "confirm", "revision": 2})
    assert response.status_code == 302 and batch.decisions.get().outcome == "EXCLUDED"
    assert restarted.get(reverse("ui:indexation_list")).status_code == 200


@pytest.mark.django_db(transaction=True)
def test_concurrent_confirmation_has_one_atomic_result(postavka):  # noqa: F811
    batch = batch_for(postavka)
    barrier = Barrier(2)

    def confirm():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return confirm_batch(batch.company_id, batch.pk, 1).pk
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: confirm(), range(2)))
    assert results == [batch.pk, batch.pk]
    assert IndexationEvent.objects.count() == 1
    assert AuditLogEntry.objects.count() == 2


def test_tampered_proposal_is_recalculated_at_confirmation(postavka):  # noqa: F811
    batch = batch_for(postavka)
    row = batch.decisions.get()
    row.proposal["new_rent"] = "999.00"
    row.save(update_fields=["proposal"])
    with pytest.raises(ValidationError, match="neveljavni"):
        confirm_batch(batch.company_id, batch.pk, 1)
    assert not IndexationEvent.objects.exists()


def test_ui_rejects_exclusion_without_reason_and_requires_csrf(postavka):  # noqa: F811
    batch = batch_for(postavka)
    url = reverse("ui:indexation_detail", args=[batch.company_id, batch.pk])
    data = {"action": "save", "revision": 1, f"exclude_{postavka.lease_id}": "on"}
    response = Client().post(url, data)
    assert (
        response.status_code == 200
        and "Razlog izključitve" in response.content.decode()
    )
    assert batch.decisions.get().exclusion_reason == ""
    assert Client(enforce_csrf_checks=True).post(url, data).status_code == 403


def test_excluded_decision_audit_and_rollback(postavka, monkeypatch):  # noqa: F811
    batch = batch_for(postavka)
    batch = save_review(
        batch.company_id, batch.pk, 1, set(), {postavka.lease_id: "Pogodbeni dogovor"}
    )

    def fail(*args, **kwargs):
        raise RuntimeError("audit failure")

    with monkeypatch.context() as context:
        context.setattr(AuditLogEntry, "save", fail)
        with pytest.raises(RuntimeError, match="audit failure"):
            confirm_batch(batch.company_id, batch.pk, batch.revision)
    assert batch.decisions.get().outcome == "PENDING"
    confirm_batch(batch.company_id, batch.pk, batch.revision)
    row = batch.decisions.get()
    audit = AuditLogEntry.objects.get(
        content_type=ContentType.objects.get_for_model(row)
    )
    assert audit.nova_vrednost == "EXCLUDED"
    assert json.loads(audit.razlog)["reason"] == "Pogodbeni dogovor"
    assert json.loads(audit.razlog)["proposal"]["effective_date"] == "2026-02-01"
    assert not IndexationEvent.objects.exists()


def test_missing_items_are_visible(postavka):  # noqa: F811
    postavka.arhivirana = True
    save_financial_fixture(postavka)
    batch = batch_for(postavka)
    row = batch.decisions.get()
    assert row.lease_item_id is None and row.proposal["blocked"]
    assert "nima veljavnih postavk" in row.proposal["explanation"]


def test_duplicate_index_input_resumes_saved_review(postavka):  # noqa: F811
    batch = batch_for(postavka)
    batch = save_review(batch.company_id, batch.pk, 1, set(), {postavka.lease_id: "Dogovor"})
    duplicate = batch_for(postavka)
    assert duplicate.pk == batch.pk and duplicate.revision == batch.revision
    assert IndexationBatch.objects.count() == 1
    assert duplicate.decisions.get().exclusion_reason == "Dogovor"


def additional_item(item, lease=None):
    return LeaseItem.objects.create(
        lease=lease or item.lease, naziv_postavke="Dodatna najemnina",
        znesek_neto_mesecno=Decimal("50.00"), vat_regime=item.vat_regime,
        indeks_ob_osnovi=Decimal("100.000"), datum_osnove_indeksacije=date(2025, 1, 1),
    )


def additional_lease(item, clause=True):
    lease = Lease.objects.get(pk=item.lease_id)
    lease.pk = None
    lease.oznaka_pogodbe = "P-2"
    lease.ima_klavzulo_indeksacije = clause
    lease.tip_pogoja_indeksacije = "VEDNO" if clause else ""
    save_financial_fixture(lease)
    return additional_item(item, lease)


def test_mixed_selection_requires_whole_contract_exclusion(postavka):  # noqa: F811
    additional_item(postavka)
    approved_item = additional_lease(postavka)
    skipped_item = additional_lease(postavka, clause=False)
    batch = batch_for(postavka)
    decisions = list(batch.decisions.order_by("pk"))
    selected = {decisions[0].pk, next(row.pk for row in decisions if row.lease_item_id == approved_item.pk)}
    batch = save_review(batch.company_id, batch.pk, 1, selected, {})
    with pytest.raises(ValidationError, match="izključitev pogodbe z razlogom"):
        confirm_batch(batch.company_id, batch.pk, batch.revision)
    assert not IndexationEvent.objects.exists() and not AuditLogEntry.objects.exists()
    assert set(batch.decisions.values_list("outcome", flat=True)) == {"PENDING"}
    with pytest.raises(ValidationError, match="obvezen"):
        save_review(batch.company_id, batch.pk, batch.revision, selected, {postavka.lease_id: " "})
    batch = save_review(batch.company_id, batch.pk, batch.revision, selected, {postavka.lease_id: "Dogovor za celotno pogodbo"})
    confirm_batch(batch.company_id, batch.pk, batch.revision)
    assert set(batch.decisions.filter(lease_id=postavka.lease_id).values_list("outcome", "reason")) == {("EXCLUDED", "Dogovor za celotno pogodbo")}
    assert batch.decisions.get(lease_item=approved_item).outcome == "APPROVED"
    skipped = batch.decisions.get(lease_item=skipped_item)
    assert skipped.outcome == "SKIPPED" and "nima klavzule" in skipped.reason
    assert IndexationEvent.objects.get().lease_item_id == approved_item.pk


def test_confirmation_rejects_inconsistent_item_exclusions(postavka):  # noqa: F811
    additional_item(postavka)
    batch = batch_for(postavka)
    row = batch.decisions.first()
    row.exclusion_reason = "Samo ena postavka"
    row.save(update_fields=["exclusion_reason"])
    with pytest.raises(ValidationError, match="vse postavke"):
        confirm_batch(batch.company_id, batch.pk, 1)
    assert not IndexationEvent.objects.exists() and not AuditLogEntry.objects.exists()


@pytest.mark.parametrize("end, included", [
    (date(2026, 1, 31), False), (date(2026, 2, 1), True), (date(2026, 2, 14), True),
])
def test_terminating_lease_uses_effective_billing_validity(postavka, end, included):  # noqa: F811
    update_financial_fixture(Lease.objects.filter(pk=postavka.lease_id),
        status=Lease.Status.ODPOVEDANA, datum_odpovedi=date(2025, 12, 1), datum_prenehanja=end,
    )
    batch = batch_for(postavka)
    assert batch.decisions.exists() == included
    if included:
        assert batch.decisions.get().proposal["eligible"]
        confirm_batch(batch.company_id, batch.pk, 1)
        assert IndexationEvent.objects.get().datum_uskladitve == date(2026, 2, 1)
    else:
        assert not IndexationEvent.objects.exists()


def test_active_fixed_term_ended_before_effective_date_is_excluded(postavka):  # noqa: F811
    update_financial_fixture(Lease.objects.filter(pk=postavka.lease_id),
        tip_trajanja=Lease.TipTrajanja.DOLOCEN_CAS, datum_konca=date(2026, 1, 31),
    )
    assert not batch_for(postavka).decisions.exists()


def test_terminating_lease_preserves_indexed_proration(postavka):  # noqa: F811
    update_financial_fixture(Lease.objects.filter(pk=postavka.lease_id),
        status=Lease.Status.ODPOVEDANA, datum_odpovedi=date(2025, 12, 1),
        datum_prenehanja=date(2026, 2, 14),
    )
    postavka.lease.refresh_from_db()
    batch = batch_for(postavka)
    confirm_batch(batch.company_id, batch.pk, 1)
    january = izracunaj_postavko(postavka, date(2026, 1, 1), date(2026, 1, 31))
    february = izracunaj_postavko(postavka, date(2026, 2, 1), date(2026, 2, 28))
    assert january.osnova == Decimal("100.05") and not january.je_sorazmerno
    assert february.osnova == Decimal("55.03") and february.je_sorazmerno
    assert february.ddv_znesek == Decimal("12.11")
    assert izracunaj_postavko(postavka, date(2026, 3, 1), date(2026, 3, 31)) is None
    event = IndexationEvent.objects.get()
    assert event.nova_vrednost == Decimal("110.06")
    postavka.refresh_from_db()
    assert postavka.znesek_neto_mesecno == Decimal("100.05")


def test_published_index_conflict_is_scoped_to_index_year(postavka):  # noqa: F811
    first = batch_for(postavka)
    confirm_batch(first.company_id, first.pk, 1)
    following = create_batch(first.company_id, 2026, Decimal("111.000"))
    confirm_batch(following.company_id, following.pk, 1)
    assert set(IndexationEvent.objects.values_list("datum_uskladitve", flat=True)) == {
        date(2026, 2, 1), date(2027, 2, 1),
    }


@pytest.mark.parametrize("outcome", ["APPROVED", "EXCLUDED", "SKIPPED"])
def test_conflicting_index_values_block_without_changing_history(postavka, outcome):  # noqa: F811
    if outcome == "SKIPPED":
        update_financial_fixture(Lease.objects.filter(pk=postavka.lease_id), ima_klavzulo_indeksacije=False, tip_pogoja_indeksacije="")
    racun_s_postavkami(postavka, mesec=1)
    cycle = MonthlyCycle.objects.create(
        obdobje=date(2026, 1, 1), datum_izdaje=date(2026, 1, 1), trenutni_korak=MonthlyCycle.Korak.POTRDITEV_PAKETA,
    )
    MonthlyBatch.objects.create(cycle=cycle, company_id=postavka.lease.unit.property.company_id,
                                calculation_snapshot={"candidates": [{"lease_id": postavka.lease_id, "lines": [{"lease_item_id": postavka.pk, "net": "100.05"}]}]})
    first = batch_for(postavka)
    conflicting = batch_for(postavka, "111.000")
    if outcome == "EXCLUDED":
        first = save_review(first.company_id, first.pk, 1, set(), {postavka.lease_id: "Dogovor"})
    confirm_batch(first.company_id, first.pk, first.revision)
    history = (list(IndexationBatch.objects.values()), list(first.decisions.values()),
               list(IndexationEvent.objects.values()), list(AuditLogEntry.objects.values()),
               list(Invoice.objects.values()), list(InvoiceLine.objects.values()), list(MonthlyBatch.objects.values()))
    for _ in range(2):
        with pytest.raises(ValidationError, match="objavljenega indeksa"):
            confirm_batch(conflicting.company_id, conflicting.pk, conflicting.revision)
        assert confirm_batch(first.company_id, first.pk, 1).pk == first.pk
    assert history == (list(IndexationBatch.objects.values()), list(first.decisions.values()),
                       list(IndexationEvent.objects.values()), list(AuditLogEntry.objects.values()),
                       list(Invoice.objects.values()), list(InvoiceLine.objects.values()), list(MonthlyBatch.objects.values()))
    assert conflicting.decisions.get().outcome == "PENDING"
    fresh_conflicting = batch_for(postavka, "112.000")
    with pytest.raises(ValidationError, match="objavljenega indeksa"):
        confirm_batch(fresh_conflicting.company_id, fresh_conflicting.pk, 1)


def test_same_published_index_can_confirm_remaining_excluded_contract(postavka):  # noqa: F811
    first = batch_for(postavka)
    first = save_review(first.company_id, first.pk, 1, set(), {postavka.lease_id: "Začasni dogovor"})
    confirm_batch(first.company_id, first.pk, first.revision)
    later = batch_for(postavka)
    assert later.pk != first.pk
    confirm_batch(later.company_id, later.pk, 1)
    assert first.decisions.get().outcome == "EXCLUDED"
    assert later.decisions.get().outcome == "APPROVED"
    assert IndexationEvent.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_conflicting_values_have_one_confirmed_winner(postavka):  # noqa: F811
    batches = [batch_for(postavka, value) for value in ["110.000", "111.000"]]
    batches = [save_review(batch.company_id, batch.pk, 1, set(), {postavka.lease_id: "Dogovor"}) for batch in batches]
    barrier = Barrier(2)

    def confirm(batch):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            try:
                return confirm_batch(batch.company_id, batch.pk, batch.revision).pk
            except ValidationError as exc:
                assert "objavljenega indeksa" in str(exc)
                return None
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(confirm, batches))
    assert sum(result is not None for result in results) == 1
    winner = IndexationBatch.objects.get(state="CONFIRMED")
    assert confirm_batch(winner.company_id, winner.pk, 1).pk == winner.pk
    assert IndexationBatch.objects.filter(state="PENDING").count() == 1
    assert AuditLogEntry.objects.count() == 1 and not IndexationEvent.objects.exists()
