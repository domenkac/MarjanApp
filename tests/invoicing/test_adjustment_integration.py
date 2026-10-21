from tests.financial_fixtures import update_financial_fixture

"""T2-C3: stored evidence, real claims, atomic issuance, and frozen compatibility."""
# ruff: noqa: F811 -- Imported pytest fixtures are injected by their names.
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date
from decimal import Decimal
from threading import Barrier

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import (
    IntegrityError,
    close_old_connections,
    connection,
    models,
    transaction,
)
from django.db.migrations.executor import MigrationExecutor

from companies.models import NumberSequence
from companies.services import potrdi_ddv_nastavitve
from contracts.adjustments import unresolved_warnings
from contracts.indexation import confirm_batch, create_batch
from contracts.models import IndexationEvent, Lease, PoracunIndeksacije
from core.models import AuditLogEntry
from invoicing import adjustments, services
from invoicing.models import (
    AdjustmentReservation,
    Invoice,
    InvoiceLine,
    MonthlyBatch,
    MonthlyCycle,
)
from rendering.contexts import pripravi_pdf_context
from rendering.eslog import pripravi_eslog_context, render_eslog, validiraj_eslog
from tests.business_fixtures import update_business_records
from tests.contracts.test_late_adjustments import (
    detect,
    event_for,
    issued_line,
    review_decision,
)
from tests.financial_fixtures import save_financial_fixture
from tests.invoicing.test_preflight import (  # noqa: F401
    _calculate,
    _freeze_ready_package,
    _freeze_unselected_package,
    potrdi_septembrski_vzorec,
    ready_company,
)
from tests.setup_helpers import first_np_number, initial_np_last, prepare_initial_setup


def setup_adjustments(data, *, new="110.00", decision="approve", months=(2, 3)):
    item, regime = data["item"], data["regime"]
    sources = [issued_line(item, month=month, net="100.00") for month in months]
    InvoiceLine.objects.filter(pk__in=[line.pk for line in sources]).update(
        ddv_rezim_sifra=regime.sifra, ddv_rekap_skupina=regime.rekapitulacijska_skupina,
    )
    event = event_for(item, new=new)
    rows = detect(event)
    for row in rows:
        if decision:
            review_decision(row, decision, "Preverjena pogodba in zgodovinski obračun." if decision == "authorize_negative" else "Zavrnjeno" if decision == "reject" else "")
    return rows, sources, event


@pytest.fixture(params=["110.00", "90.00"], ids=["positive", "negative"])
def approved_adjustments(request):
    def setup(data):
        return setup_adjustments(data, new=request.param, decision="authorize_negative" if request.param == "90.00" else "approve")
    return setup


def refresh_sample(data, approve_sample):
    potrdi_ddv_nastavitve(data["company"])
    approve_sample(data["company"], data["lease"])
    prepare_initial_setup(data["company"], data["lease"])


def draft(data, month=9):
    return services.pripravi_osnutek_np(
        data["lease"], date(2026, month, 1),
        date(2026, month, 30), date(2026, month, 10),
    ).invoice


def audit_count(row, event):
    return AuditLogEntry.objects.filter(
        content_type=ContentType.objects.get_for_model(row), object_id=row.pk,
        razlog__contains=f"INDEXATION_ADJUSTMENT_{event}",
    ).count()


@pytest.mark.parametrize("path", ["direct", "monthly"])
def test_two_periods_consumed_once_with_stored_vat_and_artifacts(
    ready_company, potrdi_septembrski_vzorec, path, settings, tmp_path, approved_adjustments,
):
    data = ready_company
    rows, sources, event = approved_adjustments(data)
    refresh_sample(data, potrdi_septembrski_vzorec)
    original_artifact = tmp_path / "historical.pdf"
    original_artifact.write_bytes(b"immutable historical PDF")
    original_archive = tmp_path / "historical-archive.pdf"
    original_archive.write_bytes(original_artifact.read_bytes())
    Invoice.objects.filter(pk__in=[line.invoice_id for line in sources]).update(
        rendered_pdf_path="historical.pdf", checksum="historical-pdf-checksum",
        rendered_eslog_path="historical.xml", rendered_eslog_checksum="historical-xml-checksum",
    )
    originals = list(Invoice.objects.filter(pk__in=[line.invoice_id for line in sources]).values())
    source_lines = list(InvoiceLine.objects.filter(pk__in=[line.pk for line in sources]).values())
    if path == "direct":
        invoice = draft(data, month=4)  # First future NP, after February and March.
        assert all(row.vkljucen_v_racun_id is None for row in rows)
        assert AdjustmentReservation.objects.filter(invoice=invoice).count() == 2
        invoice = services.potrdi_osnutek_np(invoice)
        retry = lambda: services.potrdi_osnutek_np(Invoice(pk=invoice.pk))
    else:
        candidate, review = _freeze_ready_package(data)
        frozen = deepcopy(review.batch.calculation_snapshot)
        assert [item["id"] for item in candidate["adjustments"]] == [row.pk for row in rows]
        assert AdjustmentReservation.objects.filter(review=review).count() == 2
        invoice, reused = services.materializiraj_zamrznjenega_mesecnega_kandidata(
            review.batch_id, review.lease_id, review.candidate_fingerprint,
        )
        assert not reused
        retry = lambda: services.materializiraj_zamrznjenega_mesecnega_kandidata(
            review.batch_id, review.lease_id, review.candidate_fingerprint,
        )[0]
        review.batch.refresh_from_db()
        assert review.batch.calculation_snapshot == frozen
    net = event.nova_vrednost + sum(row.znesek_poracuna for row in rows)
    assert invoice.osnova_skupaj == net
    assert invoice.ddv_skupaj == net * Decimal("0.22")
    assert invoice.skupaj_z_ddv == net * Decimal("1.22")
    assert invoice.invoiceline_set.filter(adjustment__isnull=False).count() == 2
    for row, source in zip(rows, sources, strict=True):
        row.refresh_from_db()
        line = invoice.invoiceline_set.get(adjustment=row)
        assert row.vkljucen_v_racun_id == invoice.pk
        assert line.ddv_stopnja == source.ddv_stopnja
        assert line.ddv_znesek == row.znesek_poracuna * Decimal("0.22")
        assert f"#{source.pk}" in line.opis and source.invoice.stevilka in line.opis
        assert audit_count(row, "RESERVED") == audit_count(row, "CONSUMED") == 1
    pdf = pripravi_pdf_context(invoice)
    eslog = pripravi_eslog_context(invoice)
    assert len(pdf.vrstice) == len(eslog.vrstice) == 3
    xml = render_eslog(invoice)
    validiraj_eslog(xml)
    assert str(invoice.skupaj_z_ddv).encode() in xml and b"Pora" in xml
    assert not Invoice.objects.filter(document_type__in=["STORNO", "DOBROPIS"]).exists()
    # Retry remains valid even if today's event/source context later changes.
    IndexationEvent.objects.filter(pk=event.pk).update(indeks_vrednost=Decimal("111.000"))
    assert retry().pk == invoice.pk
    assert unresolved_warnings(data["company"].pk) == []
    assert NumberSequence.objects.get(company=data["company"], tip="NP").zadnja_stevilka == initial_np_last(data["company"]) + 1
    assert list(Invoice.objects.filter(pk__in=[line.invoice_id for line in sources]).values()) == originals
    assert list(InvoiceLine.objects.filter(pk__in=[line.pk for line in sources]).values()) == source_lines
    assert original_artifact.read_bytes() == b"immutable historical PDF"
    assert original_archive.read_bytes() == b"immutable historical PDF"


@pytest.mark.parametrize("path", ["direct", "monthly"])
def test_number_and_consumption_rollback_and_restart(
    ready_company, potrdi_septembrski_vzorec, monkeypatch, path, approved_adjustments,
):
    data = ready_company
    rows, _, _ = approved_adjustments(data)
    refresh_sample(data, potrdi_septembrski_vzorec)
    if path == "direct":
        invoice = draft(data)
        issue = lambda: services.potrdi_osnutek_np(invoice)
    else:
        _, review = _freeze_ready_package(data)
        issue = lambda: services.materializiraj_zamrznjenega_mesecnega_kandidata(
            review.batch_id, review.lease_id, review.candidate_fingerprint,
        )[0]
    original = adjustments.consume

    def fail_after_consumption(invoice, reservations):
        original(invoice, reservations)
        raise RuntimeError("failure after numbering and consumption")

    monkeypatch.setattr(adjustments, "consume", fail_after_consumption)
    with pytest.raises(RuntimeError, match="failure after numbering"):
        issue()
    assert NumberSequence.objects.get(company=data["company"], tip="NP").zadnja_stevilka == initial_np_last(data["company"])
    assert not PoracunIndeksacije.objects.filter(vkljucen_v_racun__isnull=False).exists()
    assert not AdjustmentReservation.objects.filter(consumed_at__isnull=False).exists()
    assert all(audit_count(row, "CONSUMED") == 0 for row in rows)
    monkeypatch.setattr(adjustments, "consume", original)
    assert issue().stevilka == first_np_number(data["company"])


@pytest.mark.parametrize("owner", ["direct", "monthly"])
def test_competing_draft_and_frozen_candidate_cannot_claim(
    ready_company, potrdi_septembrski_vzorec, owner, approved_adjustments,
):
    data = ready_company
    approved_adjustments(data)
    refresh_sample(data, potrdi_septembrski_vzorec)
    candidate = _calculate(data)[0]
    services.premakni_mesecni_cikel(data["cycle"], MonthlyCycle.Korak.PAKETNI_PREGLED)
    review = services.uskladi_preglede_mesecnega_cikla(data["cycle"])[0]
    services.potrdi_izjemo_kandidata(
        data["cycle"], lease_id=review.lease_id,
        candidate_fingerprint=candidate["candidate_fingerprint"],
    )
    if owner == "direct":
        invoice = draft(data)
        with pytest.raises(ValidationError, match="lastnika"):
            services.zamrzni_paket_za_potrditev(data["cycle"])
        with pytest.raises(ValidationError, match="rezerviran"):
            draft(data, month=10)
        assert services.potrdi_osnutek_np(invoice).stevilka == first_np_number(data["company"])
    else:
        services.zamrzni_paket_za_potrditev(data["cycle"])
        with pytest.raises(ValidationError, match="rezerviran"):
            draft(data)
        assert services.izdaj_zamrznjeni_mesecni_paket(data["cycle"])[0]["ok"]
    assert AdjustmentReservation.objects.count() == 2


@pytest.mark.django_db(transaction=True)
def test_real_concurrent_direct_and_monthly_claims(ready_company, potrdi_septembrski_vzorec, approved_adjustments):
    data = ready_company
    approved_adjustments(data)
    refresh_sample(data, potrdi_septembrski_vzorec)
    candidate = _calculate(data)[0]
    services.premakni_mesecni_cikel(data["cycle"], MonthlyCycle.Korak.PAKETNI_PREGLED)
    review = services.uskladi_preglede_mesecnega_cikla(data["cycle"])[0]
    services.potrdi_izjemo_kandidata(
        data["cycle"], lease_id=review.lease_id,
        candidate_fingerprint=candidate["candidate_fingerprint"],
    )
    barrier = Barrier(2)

    def claim(path):
        close_old_connections()
        barrier.wait(timeout=10)
        try:
            if path == "direct":
                return path, draft(data).pk
            return path, services.zamrzni_paket_za_potrditev(data["cycle"]).pk
        except ValidationError:
            return path, None
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ["direct", "monthly"]))
    assert sum(result is not None for _, result in results) == 1
    assert AdjustmentReservation.objects.count() == 2
    winning = next(path for path, result in results if result is not None)
    if winning == "direct":
        issued = services.potrdi_osnutek_np(Invoice.objects.get(pk=results[0][1]))
    else:
        issued = services.izdaj_zamrznjeni_mesecni_paket(data["cycle"])[0]["invoice"]
    assert AdjustmentReservation.objects.filter(consumed_invoice=issued).count() == 2


@pytest.mark.parametrize("mutation", ["source", "vat", "recipient", "approval", "line", "reservation"])
@pytest.mark.parametrize("path", ["direct", "monthly"])
def test_changed_evidence_or_reservation_blocks_before_numbering(
    ready_company, potrdi_septembrski_vzorec, mutation, path, approved_adjustments,
):
    data = ready_company
    rows, sources, _ = approved_adjustments(data)
    refresh_sample(data, potrdi_septembrski_vzorec)
    if path == "direct":
        invoice = draft(data)
        issue = lambda: services.potrdi_osnutek_np(invoice)
    else:
        _, review = _freeze_ready_package(data)
        issue = lambda: services.materializiraj_zamrznjenega_mesecnega_kandidata(
            review.batch_id, review.lease_id, review.candidate_fingerprint,
        )
    if mutation == "source":
        InvoiceLine.objects.filter(pk=sources[0].pk).update(opis="Changed evidence")
    elif mutation == "vat":
        update_financial_fixture(type(data["regime"]).objects.filter(pk=data["regime"].pk), stopnja=Decimal("9.50"))
    elif mutation == "recipient":
        update_business_records(type(data["tenant"]).objects.filter(pk=data["tenant"].pk), naziv="Other recipient")
    elif mutation == "approval":
        models.QuerySet(model=PoracunIndeksacije).filter(pk=rows[0].pk).update(status="ZAVRNJEN")
    elif mutation == "line":
        if path == "direct":
            invoice.invoiceline_set.filter(adjustment=rows[0]).update(osnova=Decimal("11.00"), skupaj=Decimal("13.20"))
        else:
            AdjustmentReservation.objects.filter(adjustment=rows[0]).update(evidence={})
    else:
        AdjustmentReservation.objects.filter(adjustment=rows[0]).delete()
    with pytest.raises((ValidationError, ValueError)):
        issue()
    assert NumberSequence.objects.get(company=data["company"], tip="NP").zadnja_stevilka == initial_np_last(data["company"])
    assert not AdjustmentReservation.objects.filter(consumed_at__isnull=False).exists()


@pytest.mark.parametrize("case", ["pending", "rejected", "negative", "unresolved", "legacy"])
def test_unsafe_items_never_consumed(ready_company, potrdi_septembrski_vzorec, case):
    data = ready_company
    rows, _, _ = setup_adjustments(
        data, new="90.00" if case == "negative" else "110.00",
        decision="reject" if case == "rejected" else None,
    )
    if case in {"unresolved", "legacy"}:
        models.QuerySet(model=PoracunIndeksacije).filter(pk__in=[row.pk for row in rows]).update(
            evidence_state="UNRESOLVED" if case == "unresolved" else "LEGACY_UNVERIFIED",
        )
    refresh_sample(data, potrdi_septembrski_vzorec)
    invoice = services.potrdi_osnutek_np(draft(data))
    assert invoice.invoiceline_set.count() == 1
    assert not AdjustmentReservation.objects.exists()
    assert not PoracunIndeksacije.objects.filter(vkljucen_v_racun__isnull=False).exists()


def test_no_regular_rent_keeps_adjustments_outstanding(ready_company, potrdi_septembrski_vzorec):
    data = ready_company
    rows, _, _ = setup_adjustments(data)
    update_financial_fixture(type(data["item"]).objects.filter(pk=data["item"].pk), datum_prenehanja_postavke=date(2026, 3, 31))
    assert draft(data) is None
    assert not AdjustmentReservation.objects.exists()
    assert all(row.vkljucen_v_racun_id is None for row in rows)


def test_database_unique_claim_owner_and_line_constraints(ready_company, potrdi_septembrski_vzorec):
    data = ready_company
    rows, _, _ = setup_adjustments(data)
    refresh_sample(data, potrdi_septembrski_vzorec)
    invoice = draft(data)
    with pytest.raises(IntegrityError), transaction.atomic():
        AdjustmentReservation.objects.create(adjustment=rows[0], invoice=invoice, evidence={})
    with pytest.raises(IntegrityError), transaction.atomic():
        AdjustmentReservation.objects.filter(adjustment=rows[0]).update(invoice=None)
    line = invoice.invoiceline_set.get(adjustment=rows[0])
    line.pk = None
    with pytest.raises(IntegrityError), transaction.atomic():
        line.save(force_insert=True)


def test_regular_delta_and_first_actual_indexed_rent_review(ready_company, potrdi_septembrski_vzorec):
    data = ready_company
    setup_adjustments(data)
    refresh_sample(data, potrdi_septembrski_vzorec)
    candidate = _calculate(data)[0]
    assert "PRVA_INDEKSACIJA" in {item["code"] for item in candidate["exceptions"]}
    invoice = draft(data, month=4)
    delta = services.primerjaj_delta_np(invoice)
    assert delta.obstaja_razlika
    assert "VZROK_NEZNAN" not in {item.koda for item in delta.razlage}
    services.potrdi_osnutek_np(invoice)
    next_draft = draft(data, month=9)
    delta = services.primerjaj_delta_np(next_draft)
    assert not delta.obstaja_razlika
    assert not services._first_indexation_exceptions(list(next_draft.invoiceline_set.all()), date(2026, 9, 1), date(2026, 9, 30))


@pytest.mark.parametrize("version", [3, 4])
@pytest.mark.parametrize("issued", [False, True])
def test_legacy_frozen_payloads_unchanged(ready_company, version, issued):
    data = ready_company
    candidate, review = _freeze_ready_package(data)
    if issued:
        services.izdaj_zamrznjeni_mesecni_paket(data["cycle"])
    review.batch.refresh_from_db()
    snapshot = deepcopy(review.batch.calculation_snapshot)
    snapshot["schema_version"] = version
    candidate = snapshot["candidates"][0]
    candidate.pop("adjustments")
    for line in candidate["lines"]:
        for key in ("adjustment_id", "adjustment_snapshot", "rent_indexation_event_id", "vat_eslog_code"):
            line.pop(key)
        if version == 3:
            line.pop("vat_regime_code")
            line.pop("requires_full_deduction")
    if version == 3:
        candidate["tenant"].pop("full_deduction")
        if issued:
            Invoice.objects.update(prejemnik_polni_odbitek=None)
            InvoiceLine.objects.update(ddv_rezim_sifra="", ddv_zahteva_polni_odbitek=None)
    candidate.pop("candidate_fingerprint")
    candidate["candidate_fingerprint"] = services._candidate_fingerprint(
        candidate, company=snapshot["company"], period_start=snapshot["period_start"],
        period_end=snapshot["period_end"], issue_date=snapshot["issue_date"], schema_version=version,
    )
    review.candidate_fingerprint = candidate["candidate_fingerprint"]
    review.save(update_fields=["candidate_fingerprint"])
    review.batch.calculation_schema_version = version
    review.batch.calculation_snapshot = snapshot
    review.batch.save()
    if issued:
        Invoice.objects.update(eslog_konfiguracija_snapshot=None)
        InvoiceLine.objects.update(ddv_eslog_koda="")
    result = services.izdaj_zamrznjeni_mesecni_paket(data["cycle"])[0]
    assert result["ok"] is (issued or version == 4), result
    review.batch.refresh_from_db()
    assert review.batch.calculation_snapshot == snapshot


@pytest.mark.parametrize("index", ["110.000", "90.000"])
@pytest.mark.parametrize("problem", [None, "net", "period", "vat"])
def test_late_batch_approval_only_with_complete_verified_sources(ready_company, index, problem):
    data = ready_company
    item, lease = data["item"], data["lease"]
    lease.ima_klavzulo_indeksacije = True
    lease.tip_pogoja_indeksacije = Lease.TipPogojaIndeksacije.VEDNO
    save_financial_fixture(lease)
    item.indeks_ob_osnovi = Decimal("100.000")
    item.datum_osnove_indeksacije = date(2026, 1, 1)
    save_financial_fixture(item)
    for month in (2, 3):
        line = issued_line(item, month=month, net="95.00" if problem == "net" else "100.00", proportional=problem == "period")
        InvoiceLine.objects.filter(pk=line.pk).update(
            ddv_rezim_sifra=data["regime"].sifra,
            ddv_rekap_skupina=data["regime"].rekapitulacijska_skupina,
        )
        if problem == "vat":
            InvoiceLine.objects.filter(pk=line.pk).update(ddv_stopnja=Decimal("9.50"))
    before = list(Invoice.objects.values()), list(InvoiceLine.objects.values())
    batch = create_batch(data["company"].pk, 2025, Decimal(index))
    if problem:
        assert not batch.decisions.get().proposal["eligible"]
        with pytest.raises(ValidationError, match="Blokirana"):
            confirm_batch(batch.company_id, batch.pk, batch.revision)
        assert not PoracunIndeksacije.objects.exists()
        assert before == (list(Invoice.objects.values()), list(InvoiceLine.objects.values()))
        return
    assert batch.decisions.get().proposal["eligible"]
    confirm_batch(batch.company_id, batch.pk, batch.revision)
    assert PoracunIndeksacije.objects.count() == 2
    state = "MANUAL_REVIEW" if index == "90.000" else "VERIFIED"
    assert not PoracunIndeksacije.objects.exclude(evidence_state=state).exists()
    if index == "90.000":
        for row in PoracunIndeksacije.objects.all():
            assert row.znesek_poracuna == Decimal("-10.00")
            review_decision(row, "authorize_negative", "Preverjena pogodba in zgodovinski obračun.")
    assert before == (list(Invoice.objects.values()), list(InvoiceLine.objects.values()))


@pytest.mark.parametrize("path", ["direct", "monthly"])
@pytest.mark.parametrize("new", ["110.00", "90.00"])
def test_approval_after_draft_or_freeze_cannot_be_silently_omitted(
    ready_company, potrdi_septembrski_vzorec, path, new,
):
    data = ready_company
    rows, _, _ = setup_adjustments(data, new=new, decision=None)
    refresh_sample(data, potrdi_septembrski_vzorec)
    if path == "direct":
        invoice = draft(data)
        issue = lambda: services.potrdi_osnutek_np(invoice)
    else:
        _, review = _freeze_ready_package(data)
        frozen = deepcopy(review.batch.calculation_snapshot)
        issue = lambda: services.materializiraj_zamrznjenega_mesecnega_kandidata(
            review.batch_id, review.lease_id, review.candidate_fingerprint,
        )
    for row in rows:
        review_decision(row, "authorize_negative" if new == "90.00" else "approve", "Preverjena pogodba in zgodovinski obračun." if new == "90.00" else "")
    with pytest.raises(ValidationError, match="Manjka potrjen poračun"):
        issue()
    assert NumberSequence.objects.get(company=data["company"], tip="NP").zadnja_stevilka == initial_np_last(data["company"])
    if path == "monthly":
        review.batch.refresh_from_db()
        assert review.batch.calculation_snapshot == frozen


def test_unselected_frozen_candidate_leaves_adjustments_outstanding(
    ready_company, potrdi_septembrski_vzorec,
):
    data = ready_company
    rows, _, _ = setup_adjustments(data)
    refresh_sample(data, potrdi_septembrski_vzorec)
    _, review = _freeze_unselected_package(data)
    assert not AdjustmentReservation.objects.exists()
    invoice, _ = services.materializiraj_zamrznjenega_mesecnega_kandidata(
        review.batch_id, review.lease_id, review.candidate_fingerprint,
    )
    assert invoice.stevilka is None
    assert invoice.invoiceline_set.filter(adjustment__isnull=False).count() == 0
    assert all(row.vkljucen_v_racun_id is None for row in rows)
    assert services.potrdi_osnutek_np(draft(data, month=10)).stevilka == first_np_number(data["company"])


def test_adjustment_only_draft_is_blocked(ready_company, potrdi_septembrski_vzorec):
    data = ready_company
    setup_adjustments(data)
    refresh_sample(data, potrdi_septembrski_vzorec)
    invoice = draft(data)
    invoice.invoiceline_set.filter(adjustment__isnull=True).delete()
    with pytest.raises(ValidationError, match="Samostojen"):
        services.potrdi_osnutek_np(invoice)
    assert NumberSequence.objects.get(company=data["company"], tip="NP").zadnja_stevilka == initial_np_last(data["company"])


@pytest.mark.parametrize("path", ["direct", "monthly"])
@pytest.mark.django_db(transaction=True)
def test_real_concurrent_issuance_is_idempotent(
    ready_company, potrdi_septembrski_vzorec, path, approved_adjustments,
):
    data = ready_company
    rows, _, _ = approved_adjustments(data)
    refresh_sample(data, potrdi_septembrski_vzorec)
    if path == "direct":
        invoice = draft(data)
    else:
        _, review = _freeze_ready_package(data)
    barrier = Barrier(2)

    def issue(_):
        close_old_connections()
        barrier.wait(timeout=10)
        try:
            if path == "direct":
                return services.potrdi_osnutek_np(Invoice(pk=invoice.pk)).pk
            return services.materializiraj_zamrznjenega_mesecnega_kandidata(
                review.batch_id, review.lease_id, review.candidate_fingerprint,
            )[0].pk
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        issued_ids = list(pool.map(issue, range(2)))
    assert len(set(issued_ids)) == 1
    assert NumberSequence.objects.get(company=data["company"], tip="NP").zadnja_stevilka == initial_np_last(data["company"]) + 1
    assert all(audit_count(row, "CONSUMED") == 1 for row in rows)


def test_competing_frozen_candidates_roll_back_all_claims(ready_company, potrdi_septembrski_vzorec, approved_adjustments):
    data = ready_company
    approved_adjustments(data)
    refresh_sample(data, potrdi_septembrski_vzorec)
    candidate = _calculate(data)[0]
    snapshot = data["batch"].calculation_snapshot
    other = deepcopy(candidate)
    other.pop("candidate_fingerprint")
    other["unit_label"] = "Competing candidate"
    other["candidate_fingerprint"] = services._candidate_fingerprint(
        other, company=snapshot["company"], period_start=snapshot["period_start"],
        period_end=snapshot["period_end"], issue_date=snapshot["issue_date"],
    )
    snapshot["candidates"].append(other)
    data["batch"].save(update_fields=["calculation_snapshot"])
    services.premakni_mesecni_cikel(data["cycle"], MonthlyCycle.Korak.PAKETNI_PREGLED)
    reviews = services.uskladi_preglede_mesecnega_cikla(data["cycle"])
    for review in reviews:
        services.potrdi_izjemo_kandidata(
            data["cycle"], lease_id=review.lease_id, candidate_fingerprint=review.candidate_fingerprint,
        )
    with pytest.raises(ValidationError, match="lastnika"):
        services.zamrzni_paket_za_potrditev(data["cycle"])
    assert not AdjustmentReservation.objects.exists()
    data["cycle"].refresh_from_db()
    assert data["cycle"].trenutni_korak == MonthlyCycle.Korak.PAKETNI_PREGLED


def test_changed_source_before_reservation_needs_new_review(ready_company, potrdi_septembrski_vzorec):
    data = ready_company
    setup_adjustments(data)
    refresh_sample(data, potrdi_septembrski_vzorec)
    source = InvoiceLine.objects.filter(invoice__obdobje_od=date(2026, 2, 1)).get()
    InvoiceLine.objects.filter(pk=source.pk).update(opis="Changed after approval")
    with pytest.raises(ValidationError, match="zastarela|skladna"):
        draft(data)
    assert not AdjustmentReservation.objects.exists()


def test_approved_flag_without_decision_audit_is_not_consumed(ready_company, potrdi_septembrski_vzorec):
    data = ready_company
    rows, _, _ = setup_adjustments(data, decision=None)
    refresh_sample(data, potrdi_septembrski_vzorec)
    models.QuerySet(model=PoracunIndeksacije).filter(pk__in=[row.pk for row in rows]).update(status="POTRJEN")
    with pytest.raises(ValidationError, match="Potrditev"):
        draft(data)
    assert not AdjustmentReservation.objects.exists()


def test_consumption_cannot_be_cleared_through_ordinary_model_save(ready_company, potrdi_septembrski_vzorec):
    data = ready_company
    rows, _, _ = setup_adjustments(data)
    refresh_sample(data, potrdi_septembrski_vzorec)
    services.potrdi_osnutek_np(draft(data))
    rows[0].refresh_from_db()
    rows[0].vkljucen_v_racun = None
    with pytest.raises(ValidationError, match="revidirano"):
        rows[0].save(update_fields=["vkljucen_v_racun"])


@pytest.mark.django_db(transaction=True)
def test_additive_migration_preserves_evidence_frozen_payloads_and_issued_artifacts(ready_company):
    data = ready_company
    setup_adjustments(data)
    Invoice.objects.update(rendered_pdf_path="original.pdf", checksum="original-checksum")
    data["batch"].calculation_schema_version = 4
    data["batch"].calculation_snapshot = {"schema_version": 4, "frozen": "unchanged"}
    data["batch"].save()
    before_invoices = list(Invoice.objects.values())
    before_evidence = list(PoracunIndeksacije.objects.values())
    before_batch = deepcopy(data["batch"].calculation_snapshot)
    latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
    previous = [(app, "0012_vat_eligibility_snapshots") if app == "invoicing" else (app, name)
                for app, name in latest]
    try:
        MigrationExecutor(connection).migrate(previous)
        old_apps = MigrationExecutor(connection).loader.project_state(previous).apps
        old_lines = list(old_apps.get_model("invoicing", "InvoiceLine").objects.values())
        MigrationExecutor(connection).migrate(latest)
        assert list(Invoice.objects.values()) == before_invoices
        assert list(PoracunIndeksacije.objects.values()) == before_evidence
        data["batch"].refresh_from_db()
        assert data["batch"].calculation_snapshot == before_batch
        for old, new in zip(old_lines, InvoiceLine.objects.values(), strict=True):
            assert {key: new[key] for key in old} == old
            assert new["adjustment_id"] is None and new["adjustment_snapshot"] == {}
        assert not AdjustmentReservation.objects.exists()
    finally:
        MigrationExecutor(connection).migrate(latest)


@pytest.mark.parametrize("prorated", [False, True])
def test_first_actual_use_is_tracked_for_no_change_and_prorated_rent(
    ready_company, potrdi_septembrski_vzorec, prorated,
):
    data = ready_company
    setup_adjustments(data, new="110.00" if prorated else "100.00", decision=None)
    if prorated:
        update_financial_fixture(type(data["item"]).objects.filter(pk=data["item"].pk), datum_zacetka_postavke=date(2026, 9, 15))
    refresh_sample(data, potrdi_septembrski_vzorec)
    candidate = services.izracunaj_kandidata(data["lease"], date(2026, 9, 1), date(2026, 9, 30), date(2026, 9, 10))
    assert "PRVA_INDEKSACIJA" in {item.koda for item in candidate.izjeme}
    first = services.potrdi_osnutek_np(draft(data))
    assert first.invoiceline_set.get().rent_indexation_event_id is not None
    assert first.invoiceline_set.get().je_sorazmerno is prorated
    next_candidate = services.izracunaj_kandidata(data["lease"], date(2026, 10, 1), date(2026, 10, 31), date(2026, 10, 10))
    assert "PRVA_INDEKSACIJA" not in {item.koda for item in next_candidate.izjeme}


def test_reservation_and_audit_roll_back_together(ready_company, potrdi_septembrski_vzorec, monkeypatch, approved_adjustments):
    data = ready_company
    rows, _, _ = approved_adjustments(data)
    refresh_sample(data, potrdi_septembrski_vzorec)
    original_count = Invoice.objects.count()

    def audit_failure(*args, **kwargs):
        raise RuntimeError("reservation audit failed")

    monkeypatch.setattr(adjustments, "_audit", audit_failure)
    with pytest.raises(RuntimeError, match="reservation audit"):
        draft(data)
    assert Invoice.objects.count() == original_count
    assert not AdjustmentReservation.objects.exists()
    assert all(audit_count(row, "RESERVED") == 0 for row in rows)


@pytest.mark.parametrize("intact", [True, False])
def test_late_approval_of_completed_monthly_source_requires_exact_materialization(
    ready_company, potrdi_septembrski_vzorec, intact,
):
    data = ready_company
    _, review = _freeze_ready_package(data)
    original, _ = services.materializiraj_zamrznjenega_mesecnega_kandidata(
        review.batch_id, review.lease_id, review.candidate_fingerprint,
    )
    data["cycle"].status = MonthlyCycle.Status.ZAKLJUCEN
    data["cycle"].trenutni_korak = MonthlyCycle.Korak.ZAKLJUCEK
    data["cycle"].save()
    frozen = deepcopy(review.batch.calculation_snapshot)
    lease, item = data["lease"], data["item"]
    lease.ima_klavzulo_indeksacije = True
    lease.tip_pogoja_indeksacije = Lease.TipPogojaIndeksacije.VEDNO
    save_financial_fixture(lease)
    item.indeks_ob_osnovi = Decimal("100.000")
    item.datum_osnove_indeksacije = date(2026, 1, 1)
    save_financial_fixture(item)
    if not intact:
        original.invoiceline_set.update(opis="Changed issued source")
    before = Invoice.objects.values().get(pk=original.pk), list(original.invoiceline_set.values())
    batch = create_batch(data["company"].pk, 2025, Decimal("110.000"))
    assert batch.decisions.get().proposal["eligible"] is intact
    if intact:
        confirm_batch(batch.company_id, batch.pk, batch.revision)
        row = PoracunIndeksacije.objects.get()
        assert row.evidence_state == "VERIFIED"
        review_decision(row, "approve")
        refresh_sample(data, potrdi_septembrski_vzorec)
        data["cycle"] = MonthlyCycle.objects.create(
            obdobje=date(2026, 10, 1), datum_izdaje=date(2026, 10, 10), trenutni_korak=2,
        )
        data["batch"] = MonthlyBatch.objects.create(cycle=data["cycle"], company=data["company"])
        _calculate(data)
        services.premakni_mesecni_cikel(data["cycle"], MonthlyCycle.Korak.PAKETNI_PREGLED)
        next_review = services.uskladi_preglede_mesecnega_cikla(data["cycle"])[0]
        services.potrdi_izjemo_kandidata(
            data["cycle"], lease_id=next_review.lease_id,
            candidate_fingerprint=next_review.candidate_fingerprint,
        )
        services.zamrzni_paket_za_potrditev(data["cycle"])
        next_invoice, _ = services.materializiraj_zamrznjenega_mesecnega_kandidata(
            next_review.batch_id, next_review.lease_id, next_review.candidate_fingerprint,
        )
        assert next_invoice.stevilka == "NP-00002"
        assert next_invoice.osnova_skupaj == Decimal("120.00")
    else:
        with pytest.raises(ValidationError, match="Blokirana"):
            confirm_batch(batch.company_id, batch.pk, batch.revision)
    assert before == (Invoice.objects.values().get(pk=original.pk), list(original.invoiceline_set.values()))
    review.batch.refresh_from_db()
    assert review.batch.calculation_snapshot == frozen


@pytest.mark.parametrize("through_deletion", [False, True])
def test_cancel_reserved_draft_releases_claims_and_preserves_approval(
    ready_company, potrdi_septembrski_vzorec, through_deletion, approved_adjustments,
):
    data = ready_company
    rows, _, _ = approved_adjustments(data)
    refresh_sample(data, potrdi_septembrski_vzorec)
    invoice = draft(data)
    invoice_id = invoice.pk
    before = list(PoracunIndeksacije.objects.values())
    tokens = [adjustments.evidence(row) for row in adjustments.outstanding(invoice.lease_id, invoice.obdobje_od)]
    cancel = services.izbrisi_racun if through_deletion else services.preklici_osnutek_np
    cancel(invoice, razlog="Napačno izbrano obdobje")
    assert not Invoice.objects.filter(pk=invoice_id).exists()
    assert not InvoiceLine.objects.filter(invoice_id=invoice_id).exists()
    assert not AdjustmentReservation.objects.exists()
    assert list(PoracunIndeksacije.objects.values()) == before
    with pytest.raises(ValidationError, match="skladnega"):
        cancel(Invoice(pk=invoice_id), razlog="Drugačen razlog")
    for row in rows:
        assert audit_count(row, "RELEASED") == 1
        audit = AuditLogEntry.objects.filter(
            content_type=ContentType.objects.get_for_model(row), object_id=row.pk,
            razlog__contains="INDEXATION_ADJUSTMENT_RELEASED",
        ).get()
        payload = json.loads(audit.razlog)
        assert payload["invoice_id"] == invoice_id
        assert payload["reason"] == "Napačno izbrano obdobje"
        assert audit.stara_vrednost == str(invoice_id) and audit.nova_vrednost == ""
    # Both services accept retries using the old identity, even after a new owner claims.
    replacement = draft(data, month=10)
    assert replacement.pk != invoice_id
    assert [r.evidence for r in AdjustmentReservation.objects.order_by("adjustment_id")] == tokens
    cancel(Invoice(pk=invoice_id), razlog="Napačno izbrano obdobje")
    assert AdjustmentReservation.objects.filter(invoice=replacement).count() == len(rows)
    assert all(audit_count(row, "RELEASED") == 1 for row in rows)
    assert services.potrdi_osnutek_np(replacement).stevilka == first_np_number(data["company"])


@pytest.mark.parametrize("failure", ["release_audit", "deletion_audit"])
def test_cancellation_audit_failure_rolls_back_every_change(
    ready_company, potrdi_septembrski_vzorec, monkeypatch, failure, approved_adjustments,
):
    data = ready_company
    approved_adjustments(data)
    refresh_sample(data, potrdi_septembrski_vzorec)
    invoice = draft(data)
    tables = (Invoice, InvoiceLine, AdjustmentReservation, PoracunIndeksacije, AuditLogEntry)
    before = [list(model.objects.order_by("pk").values()) for model in tables]
    original = AuditLogEntry.save

    def fail_audit(audit, *args, **kwargs):
        original(audit, *args, **kwargs)
        event = json.loads(audit.razlog).get("event")
        if event == (
            "INDEXATION_ADJUSTMENT_RELEASED" if failure == "release_audit"
            else "DIRECT_NP_DRAFT_CANCELLED"
        ):
            raise RuntimeError("cancellation audit failed")

    monkeypatch.setattr(AuditLogEntry, "save", fail_audit)
    with pytest.raises(RuntimeError, match="cancellation audit failed"):
        services.izbrisi_racun(invoice, razlog="Preklic")
    assert [list(model.objects.order_by("pk").values()) for model in tables] == before
    monkeypatch.setattr(AuditLogEntry, "save", original)
    services.izbrisi_racun(invoice, razlog="Preklic")
    assert not Invoice.objects.filter(pk=invoice.pk).exists()


@pytest.mark.parametrize("reason", ["", "   ", None])
def test_reserved_draft_deletion_requires_explicit_reason(
    ready_company, potrdi_septembrski_vzorec, reason,
):
    data = ready_company
    rows, _, _ = setup_adjustments(data)
    refresh_sample(data, potrdi_septembrski_vzorec)
    invoice = draft(data)
    with pytest.raises(ValidationError, match="Razlog"):
        services.izbrisi_racun(invoice, razlog=reason)
    assert Invoice.objects.filter(pk=invoice.pk).exists()
    assert AdjustmentReservation.objects.count() == len(rows)
    assert all(audit_count(row, "RELEASED") == 0 for row in rows)


@pytest.mark.parametrize("state", ["issued", "changed_status", "consumed", "frozen"])
def test_cancellation_never_releases_issued_consumed_or_frozen_owners(
    ready_company, potrdi_septembrski_vzorec, state, approved_adjustments,
):
    data = ready_company
    rows, _, _ = approved_adjustments(data)
    refresh_sample(data, potrdi_septembrski_vzorec)
    if state == "frozen":
        _, review = _freeze_ready_package(data)
        _, _, _, prepared, fields = services._nalozi_podatke_materializacije(
            review.batch_id, review.lease_id, review.candidate_fingerprint,
        )
        invoice = Invoice.objects.create(**(fields | {"status": "OSNUTEK"}))
        InvoiceLine.objects.bulk_create([
            InvoiceLine(invoice=invoice, lease_id=invoice.lease_id, **line)
            for line in prepared["lines"]
        ])
    else:
        invoice = draft(data)
        if state in {"issued", "consumed"}:
            services.potrdi_osnutek_np(invoice)
        if state == "consumed":
            Invoice.objects.filter(pk=invoice.pk).update(stevilka=None, status="OSNUTEK")
        elif state == "changed_status":
            Invoice.objects.filter(pk=invoice.pk).update(status="ZA_PONOVNI_PREGLED")
    tables = (Invoice, InvoiceLine, AdjustmentReservation, PoracunIndeksacije, MonthlyBatch, AuditLogEntry)
    before = [list(model.objects.order_by("pk").values()) for model in tables]
    with pytest.raises((ValueError, ValidationError)):
        services.izbrisi_racun(invoice, razlog="Nedovoljen preklic")
    assert [list(model.objects.order_by("pk").values()) for model in tables] == before
    assert all(audit_count(row, "RELEASED") == 0 for row in rows)


def test_cancellation_does_not_refresh_or_change_stale_evidence(
    ready_company, potrdi_septembrski_vzorec,
):
    data = ready_company
    _, sources, _ = setup_adjustments(data)
    refresh_sample(data, potrdi_septembrski_vzorec)
    invoice = draft(data)
    InvoiceLine.objects.filter(pk=sources[0].pk).update(opis="Changed after reservation")
    before = list(PoracunIndeksacije.objects.values())
    services.preklici_osnutek_np(invoice, razlog="Zastarela dokazila")
    assert not AdjustmentReservation.objects.exists()
    assert list(PoracunIndeksacije.objects.values()) == before
    with pytest.raises(ValidationError, match="zastarela|skladna"):
        draft(data)


@pytest.mark.django_db(transaction=True)
def test_real_concurrent_cancellation_retry_has_one_release_audit(
    ready_company, potrdi_septembrski_vzorec,
):
    data = ready_company
    rows, _, _ = setup_adjustments(data)
    refresh_sample(data, potrdi_septembrski_vzorec)
    invoice = draft(data)
    barrier = Barrier(2)

    def cancel(_):
        close_old_connections()
        barrier.wait(timeout=10)
        try:
            return services.preklici_osnutek_np(Invoice(pk=invoice.pk), razlog="Preklic")
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(cancel, range(2)))
    assert sorted(results) == [False, True]
    assert all(audit_count(row, "RELEASED") == 1 for row in rows)
    assert not AdjustmentReservation.objects.exists()
    assert services.potrdi_osnutek_np(draft(data)).stevilka == first_np_number(data["company"])


def test_cancel_direct_draft_unblocks_previously_reviewed_monthly_candidate(
    ready_company, potrdi_septembrski_vzorec,
):
    data = ready_company
    rows, _, _ = setup_adjustments(data)
    refresh_sample(data, potrdi_septembrski_vzorec)
    _calculate(data)
    services.premakni_mesecni_cikel(data["cycle"], MonthlyCycle.Korak.PAKETNI_PREGLED)
    review = services.uskladi_preglede_mesecnega_cikla(data["cycle"])[0]
    services.potrdi_izjemo_kandidata(
        data["cycle"], lease_id=review.lease_id, candidate_fingerprint=review.candidate_fingerprint,
    )
    direct = draft(data)
    with pytest.raises(ValidationError, match="lastnika"):
        services.zamrzni_paket_za_potrditev(data["cycle"])
    services.izbrisi_racun(direct, razlog="Izbrana mesečna izdaja")
    services.zamrzni_paket_za_potrditev(data["cycle"])
    assert AdjustmentReservation.objects.filter(review=review).count() == len(rows)
    services.izbrisi_racun(Invoice(pk=direct.pk), razlog="Izbrana mesečna izdaja")
    assert AdjustmentReservation.objects.filter(review=review).count() == len(rows)
    invoice, reused = services.materializiraj_zamrznjenega_mesecnega_kandidata(
        review.batch_id, review.lease_id, review.candidate_fingerprint,
    )
    assert not reused and invoice.stevilka == first_np_number(data["company"])
    assert all(audit_count(row, "RELEASED") == audit_count(row, "CONSUMED") == 1 for row in rows)


@pytest.mark.django_db(transaction=True)
def test_real_concurrent_cancellation_and_issuance_never_release_consumption(
    ready_company, potrdi_septembrski_vzorec,
):
    data = ready_company
    rows, _, _ = setup_adjustments(data)
    refresh_sample(data, potrdi_septembrski_vzorec)
    invoice = draft(data)
    barrier = Barrier(2)

    def action(kind):
        close_old_connections()
        barrier.wait(timeout=10)
        try:
            if kind == "cancel":
                return kind, services.preklici_osnutek_np(Invoice(pk=invoice.pk), razlog="Preklic")
            return kind, bool(services.potrdi_osnutek_np(Invoice(pk=invoice.pk)).stevilka)
        except (Invoice.DoesNotExist, ValidationError, ValueError):
            return kind, False
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = dict(pool.map(action, ["cancel", "issue"]))
    assert sum(results.values()) == 1
    if results["cancel"]:
        assert not Invoice.objects.filter(pk=invoice.pk).exists()
        assert not AdjustmentReservation.objects.exists() and NumberSequence.objects.get(company=data["company"], tip="NP").zadnja_stevilka == initial_np_last(data["company"])
        assert all(audit_count(row, "RELEASED") == 1 and audit_count(row, "CONSUMED") == 0 for row in rows)
        assert services.potrdi_osnutek_np(draft(data)).stevilka == first_np_number(data["company"])
    else:
        invoice.refresh_from_db()
        assert invoice.stevilka == first_np_number(data["company"])
        assert AdjustmentReservation.objects.filter(consumed_invoice=invoice).count() == len(rows)
        assert all(audit_count(row, "RELEASED") == 0 and audit_count(row, "CONSUMED") == 1 for row in rows)
