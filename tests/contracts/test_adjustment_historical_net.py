import json
from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest

from contracts.models import PoracunIndeksacije
from invoicing.models import Invoice, InvoiceLine, MonthlyBatch, MonthlyCycle
from tests.contracts.test_adjustment_write_safety import audits
from tests.contracts.test_indeksacija import postavka  # noqa: F401
from tests.contracts.test_late_adjustments import (
    detect,
    event_for,
    issued_line,
    reasons,
)


@pytest.mark.parametrize(
    "billed,adjustment,state",
    [
        ("95.05", "15.01", "UNRESOLVED"),
        ("100.05", "10.01", "VERIFIED"),
        ("115.05", "-4.99", "MANUAL_REVIEW"),
    ],
)
def test_full_month_historical_net_comparison(postavka, billed, adjustment, state):  # noqa: F811
    line = issued_line(postavka, net=billed)
    row = detect(event_for(postavka))[0]
    assert row.evidence_state == state
    assert row.znesek_poracuna == Decimal(adjustment)
    calculation = row.calculation_evidence["calculation"]
    assert calculation["expected_old_period_net"] == "100.05"
    assert calculation["billed_original_net"] == billed
    assert calculation["adjustment_net"] == adjustment
    if billed != "100.05":
        reason = next(
            reason
            for reason in row.unresolved_reasons
            if reason["code"] == "HISTORICAL_NET_MISMATCH"
        )
        assert reason["expected_old_period_net"] == "100.05"
        assert reason["billed_original_net"] == billed
        assert "uskladitev" in reason["message"]
    else:
        assert row.unresolved_reasons == []
    line.refresh_from_db()
    assert line.osnova == Decimal(billed)
    assert row.vkljucen_v_racun_id is None


@pytest.mark.parametrize(
    "start,end,billed,old_expected,adjustment,state",
    [
        (15, 28, "50.03", "50.03", "5.00", "VERIFIED"),
        (15, 28, "45.03", "50.03", "10.00", "UNRESOLVED"),
        (15, 28, "50.02", "50.03", "5.01", "UNRESOLVED"),
        (15, 28, "60.03", "50.03", "-5.00", "MANUAL_REVIEW"),
        (26, 28, "10.72", "10.72", "1.07", "VERIFIED"),
    ],
)
def test_verified_partial_historical_net_comparison(
    postavka,  # noqa: F811
    start,
    end,
    billed,
    old_expected,
    adjustment,
    state,
):
    line = issued_line(postavka, net=billed, proportional=True)
    row = detect(
        event_for(postavka),
        verified_periods={
            line.pk: {
                "start": date(2026, 2, start),
                "end": date(2026, 2, end),
                "reference": "Preverjena pogodba in zgodovinsko obdobje",
            }
        },
    )[0]
    assert row.evidence_state == state
    assert row.znesek_poracuna == Decimal(adjustment)
    calculation = row.calculation_evidence["calculation"]
    assert calculation["expected_old_period_net"] == old_expected
    assert calculation["calendar_days"] == end - start + 1
    assert calculation["month_days"] == 28
    assert calculation["rounding"] == "ROUND_HALF_UP"
    assert ("HISTORICAL_NET_MISMATCH" in reasons(row)) == (billed != old_expected)


def test_manual_override_explanation_does_not_automatically_reconcile_concession(
    postavka,  # noqa: F811
):
    line = issued_line(postavka, net="95.05")
    InvoiceLine.objects.filter(pk=line.pk).update(
        razlog_rocnega_popravka="Pogodbeni popust",
        izvirno_izracunana_vrednost=Decimal("100.05"),
    )
    row = detect(event_for(postavka))[0]
    assert row.evidence_state == "UNRESOLVED"
    assert {"MANUAL_CORRECTION", "HISTORICAL_NET_MISMATCH"} <= reasons(row)
    assert row.znesek_poracuna == Decimal("15.01")


def test_historical_mismatch_retry_audit_and_immutable_snapshots(postavka):  # noqa: F811
    line = issued_line(postavka, net="95.05")
    event = event_for(postavka)
    cycle = MonthlyCycle.objects.create(
        obdobje=date(2026, 3, 1),
        datum_izdaje=date(2026, 3, 1),
        trenutni_korak=5,
    )
    MonthlyBatch.objects.create(
        company=line.invoice.company,
        cycle=cycle,
        calculation_snapshot={"frozen": "unchanged"},
    )
    models = [Invoice, InvoiceLine, MonthlyCycle, MonthlyBatch]
    snapshots = [list(model.objects.values()) for model in models]
    original = detect(event)[0]
    original_rows = list(PoracunIndeksacije.objects.values())
    history = list(audits(original).values())
    assert detect(event)[0].pk == original.pk
    assert list(PoracunIndeksacije.objects.values()) == original_rows
    assert list(audits(original).values()) == history
    assert [list(model.objects.values()) for model in models] == snapshots

    evidence, original_reasons = (
        deepcopy(original.calculation_evidence),
        deepcopy(original.unresolved_reasons),
    )
    # Test-only conflicting historical evidence; detection never edits an invoice.
    InvoiceLine.objects.filter(pk=line.pk).update(
        osnova=Decimal("90.05"),
        ddv_znesek=Decimal("19.81"),
        skupaj=Decimal("109.86"),
    )
    snapshots = [list(model.objects.values()) for model in models]
    changed = detect(event)[0]
    assert changed.evidence_state == original.evidence_state == "UNRESOLVED"
    assert changed.znesek_poracuna == Decimal("15.01")
    assert changed.calculation_evidence == evidence
    assert changed.unresolved_reasons[: len(original_reasons)] == original_reasons
    audit = audits(changed).last()
    assert audit.polje == "last_observation_fingerprint"
    assert audit.stara_vrednost == original.last_observation_fingerprint
    assert audit.nova_vrednost == changed.last_observation_fingerprint
    payload = json.loads(audit.razlog)
    assert "evidence_state" not in payload["changes"]
    assert payload["observation"]["calculation"]["expected_old_period_net"] == "100.05"
    assert payload["observation"]["calculation"]["adjustment_net"] == "20.01"
    assert [list(model.objects.values()) for model in models] == snapshots
    history = list(audits(changed).values())
    detect(event)
    assert list(audits(changed).values()) == history
