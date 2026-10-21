"""Signed historical VAT, mixed adjustments and fail-closed payable totals."""

# ruff: noqa: F811 -- Imported pytest fixtures are injected by name.
from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import models

from catalogs.models import ESLOG_KODE_DDV
from companies.models import NumberSequence
from contracts.models import LeaseItem, PoracunIndeksacije
from core.pravila import zaokrozi
from invoicing import adjustments, services
from invoicing.models import AdjustmentReservation, Invoice, InvoiceLine
from tests.contracts.test_adjustment_write_safety import audits
from tests.contracts.test_late_adjustments import (
    detect,
    event_for,
    issued_line,
    review_decision,
)
from tests.contracts.test_negative_adjustment_review import REASON
from tests.financial_fixtures import save_financial_fixture
from tests.invoicing.test_adjustment_integration import (
    draft,
    refresh_sample,
    setup_adjustments,
)
from tests.invoicing.test_preflight import (  # noqa: F401
    _freeze_ready_package,
    potrdi_septembrski_vzorec,
    ready_company,
)
from tests.setup_helpers import initial_np_last


@pytest.mark.parametrize("path", ["direct", "monthly"])
@pytest.mark.parametrize(
    "mode,rate",
    [
        ("OBDAVCENO", "22.00"),
        ("OBDAVCENO", "9.50"),
        ("OPROSCENO", "0.00"),
        ("OBRNJENA_DAVCNA", "0.00"),
    ],
)
def test_historical_signed_vat_on_regular_np(
    ready_company, potrdi_septembrski_vzorec, path, mode, rate
):
    data = ready_company
    regime = data["regime"]
    regime.stopnja = Decimal(rate)
    regime.nacin_obracuna = mode
    regime.eslog_koda = ESLOG_KODE_DDV[mode]
    regime.klavzula = "Preverjena zgodovinska klavzula" if mode != "OBDAVCENO" else ""
    save_financial_fixture(regime)
    source = issued_line(data["item"], net="100.00")
    vat = (
        zaokrozi(Decimal("100.00") * Decimal(rate) / 100)
        if mode == "OBDAVCENO"
        else Decimal("0.00")
    )
    InvoiceLine.objects.filter(pk=source.pk).update(
        ddv_stopnja=regime.stopnja,
        ddv_nacin_obracuna=mode,
        ddv_rezim_sifra=regime.sifra,
        ddv_rekap_skupina=regime.rekapitulacijska_skupina,
        ddv_klavzula=regime.klavzula,
        ddv_eslog_koda=regime.eslog_koda,
        ddv_znesek=vat,
        skupaj=Decimal("100.00") + vat,
    )
    row = detect(event_for(data["item"], new="90.00"))[0]
    review_decision(row, "authorize_negative", REASON)
    original = (
        Invoice.objects.values().get(pk=source.invoice_id),
        InvoiceLine.objects.values().get(pk=source.pk),
    )
    refresh_sample(data, potrdi_septembrski_vzorec)
    if path == "direct":
        invoice = services.potrdi_osnutek_np(draft(data))
    else:
        _, review = _freeze_ready_package(data)
        invoice, _ = services.materializiraj_zamrznjenega_mesecnega_kandidata(
            review.batch_id, review.lease_id, review.candidate_fingerprint
        )
    line = invoice.invoiceline_set.get(adjustment=row)
    assert line.osnova == Decimal("-10.00")
    assert line.ddv_znesek == (
        -Decimal("10.00") * Decimal(rate) / 100
        if mode == "OBDAVCENO"
        else Decimal("0.00")
    )
    assert line.ddv_stopnja == Decimal(rate) and line.ddv_nacin_obracuna == mode
    assert line.adjustment_snapshot["source_invoice_id"] == source.invoice_id
    assert original == (
        Invoice.objects.values().get(pk=source.invoice_id),
        InvoiceLine.objects.values().get(pk=source.pk),
    )
    assert not Invoice.objects.filter(document_type__in=["STORNO", "DOBROPIS"]).exists()


@pytest.mark.parametrize("new", ["40.00", "50.00"])
@pytest.mark.parametrize("path", ["direct", "monthly"])
def test_nonpositive_payable_is_blocked_without_dropping_adjustments(
    ready_company, potrdi_septembrski_vzorec, new, path
):
    data = ready_company
    rows, _, _ = setup_adjustments(
        data, new=new, decision="authorize_negative", months=(2,)
    )
    refresh_sample(data, potrdi_septembrski_vzorec)
    before = deepcopy(list(PoracunIndeksacije.objects.values()))
    if path == "direct":
        invoice = draft(data)
        assert invoice.invoiceline_set.filter(adjustment__isnull=False).count() == len(
            rows
        )
        issue = lambda: services.potrdi_osnutek_np(invoice)
    else:
        issue = lambda: _freeze_ready_package(data)
    with pytest.raises(ValidationError, match="pozitiven skupni znesek"):
        issue()
    assert NumberSequence.objects.get(
        company=data["company"], tip="NP"
    ).zadnja_stevilka == initial_np_last(data["company"])
    assert not AdjustmentReservation.objects.filter(consumed_at__isnull=False).exists()
    assert list(PoracunIndeksacije.objects.values()) == before
    assert adjustments.outstanding(data["lease"].pk, date(2026, 9, 1)).count() == len(
        rows
    )
    if path == "monthly":
        assert not AdjustmentReservation.objects.exists()


@pytest.mark.parametrize("path", ["direct", "monthly"])
def test_positive_and_negative_lines_keep_separate_provenance(
    ready_company, potrdi_septembrski_vzorec, path
):
    data = ready_company
    rows, _, _ = setup_adjustments(
        data, new="90.00", decision="authorize_negative", months=(2,)
    )
    item = LeaseItem.objects.create(
        lease=data["lease"],
        vat_regime=data["regime"],
        znesek_neto_mesecno=Decimal("100.00"),
        referencna_najemnina=Decimal("100.00"),
        datum_referencne_najemnine=date(2026, 1, 1),
    )
    source = issued_line(item, net="100.00")
    InvoiceLine.objects.filter(pk=source.pk).update(
        ddv_rezim_sifra=data["regime"].sifra,
        ddv_rekap_skupina=data["regime"].rekapitulacijska_skupina,
    )
    positive = detect(event_for(item, new="110.00"))[0]
    review_decision(positive, "approve")
    refresh_sample(data, potrdi_septembrski_vzorec)
    if path == "direct":
        invoice = services.potrdi_osnutek_np(draft(data))
    else:
        _, review = _freeze_ready_package(data)
        invoice, _ = services.materializiraj_zamrznjenega_mesecnega_kandidata(
            review.batch_id, review.lease_id, review.candidate_fingerprint
        )
    assert sorted(
        invoice.invoiceline_set.filter(adjustment__isnull=False).values_list(
            "osnova", flat=True
        )
    ) == [Decimal("-10.00"), Decimal("10.00")]
    assert invoice.osnova_skupaj == Decimal("200.00")
    assert {
        r.adjustment_id
        for r in AdjustmentReservation.objects.filter(consumed_invoice=invoice)
    } == {rows[0].pk, positive.pk}


def test_forged_confirmation_without_negative_authorization_is_blocked(
    ready_company, potrdi_septembrski_vzorec
):
    data = ready_company
    rows, _, _ = setup_adjustments(data, new="90.00", decision=None)
    refresh_sample(data, potrdi_septembrski_vzorec)
    models.QuerySet(model=PoracunIndeksacije).filter(
        pk__in=[r.pk for r in rows]
    ).update(status="POTRJEN")
    with pytest.raises(ValidationError, match="Potrditev"):
        draft(data)
    assert not AdjustmentReservation.objects.exists()


def test_consumed_negative_cannot_receive_another_decision(
    ready_company, potrdi_septembrski_vzorec
):
    data = ready_company
    rows, _, _ = setup_adjustments(data, new="90.00", decision="authorize_negative")
    refresh_sample(data, potrdi_septembrski_vzorec)
    invoice = services.potrdi_osnutek_np(draft(data))
    for row in rows:
        row.refresh_from_db()
        before = audits(row).count()
        with pytest.raises(ValidationError):
            review_decision(row, "authorize_negative", REASON)
        assert audits(row).count() == before
        assert row.vkljucen_v_racun_id == invoice.pk
