from contextlib import contextmanager
from datetime import date

import pytest
from django.core.exceptions import ValidationError
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from delivery.models import MonthlySummaryAttempt
from invoicing import services
from invoicing.models import MonthlyCycle
from invoicing.services import (
    NeveljavenPrehodMesecnegaCikla,
    ZakljucenoObdobje,
    zacni_ali_nadaljuj_mesecni_cikel,
    zakljuci_mesecni_cikel,
)

pytestmark = pytest.mark.django_db(transaction=True)


def _cycle():
    return MonthlyCycle.objects.create(
        obdobje=date(2026, 8, 1),
        datum_izdaje=date(2026, 8, 12),
        trenutni_korak=MonthlyCycle.Korak.ZAKLJUCEK,
    )


def _final_state(cycle, **overrides):
    state = {
        "cycle_id": cycle.pk,
        "period": cycle.obdobje,
        "delivery_complete": True,
        "archive_complete": True,
        "all_required_work_successful": True,
        "requires_attention": False,
        "headline": "Mesečni postopek uspešno zaključen",
        "prepared_count": 2,
        "sent_count": 2,
        "intentionally_unissued_count": 1,
        "failed_count": 0,
        "unknown_count": 0,
        "delivery_pending_count": 0,
        "archive_verified_count": 2,
        "archive_pending_count": 0,
        "conflict_count": 0,
        "per_company": [{"company_id": 7, "requires_attention": False}],
        "issues": [],
    }
    state.update(overrides)
    return state


def _summary(cycle, result=MonthlySummaryAttempt.Result.ACCEPTED, *, sequence=1, snapshot=None):
    return MonthlySummaryAttempt.objects.create(
        cycle=cycle,
        sequence=sequence,
        sender="owner@example.com",
        recipient="owner@example.com",
        subject="Mesečni postopek – 08/2026",
        body="Summary",
        summary_snapshot=snapshot or {"requires_attention": False},
        result=result,
        result_code="SMTP_ACCEPTED" if result == MonthlySummaryAttempt.Result.ACCEPTED else "",
        completed_at=timezone.now() if result == MonthlySummaryAttempt.Result.ACCEPTED else None,
    )


def _successful(monkeypatch, cycle):
    monkeypatch.setattr(
        services,
        "stanje_zakljucne_kontrole_mesecnega_cikla",
        lambda current: _final_state(current),
    )


def test_successful_finalization_persists_authoritative_evidence(monkeypatch):
    cycle = _cycle()
    attempt = _summary(cycle)
    _successful(monkeypatch, cycle)

    result = zakljuci_mesecni_cikel(cycle)

    result.refresh_from_db()
    assert result.status == MonthlyCycle.Status.ZAKLJUCEN
    assert result.trenutni_korak == MonthlyCycle.Korak.ZAKLJUCEK
    assert result.completed_at is not None
    assert result.completion_snapshot == {
        "schema_version": 1,
        "cycle_id": cycle.pk,
        "period": "2026-08-01",
        "headline": "Mesečni postopek uspešno zaključen",
        "all_required_work_successful": True,
        "requires_attention": False,
        "prepared_count": 2,
        "sent_count": 2,
        "intentionally_unissued_count": 1,
        "failed_count": 0,
        "unknown_count": 0,
        "delivery_pending_count": 0,
        "archive_verified_count": 2,
        "archive_pending_count": 0,
        "conflict_count": 0,
        "per_company": [{"company_id": 7, "requires_attention": False}],
        "issues": [],
        "summary_attempt_id": attempt.pk,
        "summary_attempt_sequence": 1,
        "summary_recipient": "owner@example.com",
        "summary_subject": "Mesečni postopek – 08/2026",
        "summary_completed_at": attempt.completed_at.isoformat(),
        "summary_result": "ACCEPTED",
        "summary_result_code": "SMTP_ACCEPTED",
    }


@pytest.mark.parametrize(
    "result",
    [
        None,
        MonthlySummaryAttempt.Result.PREPARED,
        MonthlySummaryAttempt.Result.SUBMISSION_STARTED,
        MonthlySummaryAttempt.Result.CONFIRMED_FAILURE,
        MonthlySummaryAttempt.Result.UNKNOWN,
    ],
)
def test_only_latest_accepted_summary_allows_closure(monkeypatch, result):
    cycle = _cycle()
    if result is not None:
        _summary(cycle, result)
    _successful(monkeypatch, cycle)

    with pytest.raises(ValidationError, match="ACCEPTED"):
        zakljuci_mesecni_cikel(cycle)

    cycle.refresh_from_db()
    assert cycle.status == MonthlyCycle.Status.V_TEKU
    assert cycle.completed_at is None and cycle.completion_snapshot is None


@pytest.mark.parametrize(
    "failure",
    [
        {"requires_attention": True, "all_required_work_successful": False, "failed_count": 1},
        {"requires_attention": True, "all_required_work_successful": False, "unknown_count": 1},
        {"requires_attention": True, "all_required_work_successful": False, "archive_pending_count": 1},
        {"requires_attention": True, "all_required_work_successful": False, "conflict_count": 1},
    ],
    ids=["failed-delivery", "unknown", "archive-incomplete", "package-conflict"],
)
def test_unresolved_final_control_state_blocks_closure(monkeypatch, failure):
    cycle = _cycle()
    _summary(cycle)
    monkeypatch.setattr(
        services,
        "stanje_zakljucne_kontrole_mesecnega_cikla",
        lambda current: _final_state(current, **failure),
    )

    with pytest.raises(ValidationError, match="končna kontrola"):
        zakljuci_mesecni_cikel(cycle)


def test_zero_selected_package_can_close(monkeypatch):
    cycle = _cycle()
    _summary(cycle)
    monkeypatch.setattr(
        services,
        "stanje_zakljucne_kontrole_mesecnega_cikla",
        lambda current: _final_state(
            current,
            prepared_count=0,
            sent_count=0,
            archive_verified_count=0,
            intentionally_unissued_count=1,
        ),
    )

    assert zakljuci_mesecni_cikel(cycle).status == MonthlyCycle.Status.ZAKLJUCEN


def test_live_final_control_runs_outside_write_transaction(monkeypatch):
    cycle = _cycle()
    _summary(cycle)

    def final_control(current):
        assert not connection.in_atomic_block
        return _final_state(current)

    monkeypatch.setattr(services, "stanje_zakljucne_kontrole_mesecnega_cikla", final_control)

    zakljuci_mesecni_cikel(cycle)


def test_cycle_race_after_live_control_fails_without_rewriting_evidence(monkeypatch):
    cycle = _cycle()
    _summary(cycle)
    _successful(monkeypatch, cycle)
    original_atomic = services.transaction.atomic

    @contextmanager
    def race_atomic():
        MonthlyCycle.objects.filter(pk=cycle.pk).update(status=MonthlyCycle.Status.ZAKLJUCEN)
        with original_atomic():
            yield

    monkeypatch.setattr(services.transaction, "atomic", race_atomic)

    with pytest.raises(NeveljavenPrehodMesecnegaCikla):
        zakljuci_mesecni_cikel(cycle)

    cycle.refresh_from_db()
    assert cycle.completed_at is None and cycle.completion_snapshot is None


def test_newer_summary_race_after_snapshot_preparation_fails(monkeypatch):
    cycle = _cycle()
    accepted = _summary(cycle)
    _successful(monkeypatch, cycle)
    original_atomic = services.transaction.atomic

    @contextmanager
    def race_atomic():
        _summary(cycle, MonthlySummaryAttempt.Result.PREPARED, sequence=2)
        with original_atomic():
            yield

    monkeypatch.setattr(services.transaction, "atomic", race_atomic)

    with pytest.raises(ValidationError, match="spremenil"):
        zakljuci_mesecni_cikel(cycle)

    assert MonthlySummaryAttempt.objects.get(pk=accepted.pk).result == MonthlySummaryAttempt.Result.ACCEPTED
    cycle.refresh_from_db()
    assert cycle.status == MonthlyCycle.Status.V_TEKU


def test_valid_closed_cycle_is_idempotent(monkeypatch):
    cycle = _cycle()
    _summary(cycle)
    _successful(monkeypatch, cycle)
    first = zakljuci_mesecni_cikel(cycle)
    timestamp = first.completed_at
    snapshot = first.completion_snapshot.copy()
    monkeypatch.setattr(
        services,
        "stanje_zakljucne_kontrole_mesecnega_cikla",
        lambda current: pytest.fail("closed cycle must not rerun live control"),
    )

    second = zakljuci_mesecni_cikel(first)

    assert second.completed_at == timestamp
    assert second.completion_snapshot == snapshot
    assert MonthlySummaryAttempt.objects.count() == 1


def test_closed_cycle_with_corrupt_evidence_fails_closed():
    cycle = _cycle()
    MonthlyCycle.objects.filter(pk=cycle.pk).update(
        status=MonthlyCycle.Status.ZAKLJUCEN,
        completed_at=timezone.now(),
        completion_snapshot={"schema_version": 1},
    )

    with pytest.raises(ValidationError, match="dokazil"):
        zakljuci_mesecni_cikel(cycle)


_DELETE = object()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("prepared_count", _DELETE),
        ("per_company", _DELETE),
        ("summary_attempt_sequence", _DELETE),
        ("summary_recipient", _DELETE),
        ("summary_completed_at", _DELETE),
        ("summary_completed_at", "not-an-iso-datetime"),
        ("summary_result", "UNKNOWN"),
        ("trenutni_korak", MonthlyCycle.Korak.POTRDITEV_PAKETA),
        ("prepared_count", -1),
        ("prepared_count", True),
    ],
    ids=[
        "missing-count",
        "missing-per-company",
        "missing-summary-sequence",
        "missing-summary-recipient",
        "missing-summary-completed-at",
        "invalid-summary-completed-at",
        "summary-not-accepted",
        "wrong-current-step",
        "negative-count",
        "boolean-count",
    ],
)
def test_closed_cycle_with_malformed_schema_v1_evidence_fails_closed(
    monkeypatch, field, value
):
    cycle = _cycle()
    _summary(cycle)
    _successful(monkeypatch, cycle)
    closed = zakljuci_mesecni_cikel(cycle)
    snapshot = closed.completion_snapshot.copy()
    update = {}
    if field == "trenutni_korak":
        update[field] = value
    elif value is _DELETE:
        snapshot.pop(field)
        update["completion_snapshot"] = snapshot
    else:
        snapshot[field] = value
        update["completion_snapshot"] = snapshot
    MonthlyCycle.objects.filter(pk=closed.pk).update(**update)

    with pytest.raises(ValidationError, match="dokazil"):
        zakljuci_mesecni_cikel(closed)


def test_finalization_updates_only_monthly_cycle(monkeypatch):
    cycle = _cycle()
    _summary(cycle)
    _successful(monkeypatch, cycle)

    with CaptureQueriesContext(connection) as queries:
        zakljuci_mesecni_cikel(cycle)

    mutations = [
        query["sql"]
        for query in queries
        if query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
    ]
    assert len(mutations) == 1
    assert mutations[0].lstrip().upper().startswith("UPDATE")
    assert '"invoicing_monthlycycle"' in mutations[0]


def test_same_period_remains_rejected_after_closure(monkeypatch):
    cycle = _cycle()
    _summary(cycle)
    _successful(monkeypatch, cycle)
    zakljuci_mesecni_cikel(cycle)

    assert services.nalozi_aktivni_mesecni_cikel() is None
    with pytest.raises(ZakljucenoObdobje):
        zacni_ali_nadaljuj_mesecni_cikel(cycle.obdobje, cycle.datum_izdaje)


def test_historic_attention_snapshot_does_not_override_current_control(monkeypatch):
    cycle = _cycle()
    _summary(cycle, snapshot={"requires_attention": True, "headline": "Mesečni postopek zahteva pozornost"})
    _successful(monkeypatch, cycle)

    closed = zakljuci_mesecni_cikel(cycle)

    assert closed.status == MonthlyCycle.Status.ZAKLJUCEN
    assert closed.completion_snapshot["headline"] == "Mesečni postopek uspešno zaključen"


def test_unsaved_cycle_cannot_be_finalized():
    cycle = MonthlyCycle(
        obdobje=date(2026, 8, 1),
        datum_izdaje=date(2026, 8, 12),
        trenutni_korak=MonthlyCycle.Korak.ZAKLJUCEK,
    )

    with pytest.raises(ValidationError, match="shranjen"):
        zakljuci_mesecni_cikel(cycle)
