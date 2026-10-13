import json
from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError

from contracts.adjustments import prepare_adjustments, unresolved_warnings
from contracts.models import PoracunIndeksacije
from core.models import AuditLogEntry
from invoicing.models import InvoiceLine
from tests.contracts.test_indeksacija import postavka  # noqa: F401
from tests.contracts.test_late_adjustments import detect, event_for, issued_line


def audits(row):
    return AuditLogEntry.objects.filter(
        content_type=ContentType.objects.get_for_model(row),
        object_id=row.pk,
    ).order_by("pk")


@pytest.mark.parametrize(
    "tamper", ["amount", "message", "remove_amount", "remove_reason", "append"]
)
def test_ordinary_save_cannot_rewrite_negative_reason_or_warning(postavka, tamper):  # noqa: F811
    line = issued_line(postavka)
    row = detect(event_for(postavka, new="90.05"))[0]
    before = deepcopy(row.unresolved_reasons)
    warning = unresolved_warnings(line.invoice.company_id)
    if tamper == "amount":
        row.unresolved_reasons[0]["amount"] = "-999.00"
    elif tamper == "message":
        row.unresolved_reasons[0]["message"] = "Spremenjeno opozorilo"
    elif tamper == "remove_amount":
        row.unresolved_reasons[0].pop("amount")
    elif tamper == "remove_reason":
        row.unresolved_reasons = []
    else:
        row.unresolved_reasons.append({"code": "MANUAL", "message": "Brez revizije"})
    with pytest.raises(ValidationError):
        row.save(update_fields=["unresolved_reasons"])
    row.refresh_from_db()
    assert row.unresolved_reasons == before
    assert unresolved_warnings(line.invoice.company_id) == warning
    assert audits(row).count() == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("evidence_state", "UNRESOLVED"),
        ("last_observation_fingerprint", "0" * 64),
        ("unresolved_reasons", [{"code": "UNAPPROVED", "message": "Brez revizije"}]),
    ],
)
@pytest.mark.parametrize("method", ["save", "update", "bulk_update"])
def test_evidence_changes_require_audited_detection(postavka, field, value, method):  # noqa: F811
    issued_line(postavka)
    row = detect(event_for(postavka))[0]
    original = getattr(row, field)
    setattr(row, field, value)
    with pytest.raises(ValidationError):
        if method == "save":
            row.save(update_fields=[field])
        elif method == "update":
            PoracunIndeksacije.objects.filter(pk=row.pk).update(**{field: value})
        else:
            PoracunIndeksacije.objects.bulk_update([row], [field])
    row.refresh_from_db()
    assert getattr(row, field) == original
    assert audits(row).count() == 1


def test_detection_preserves_negative_payloads_and_appends_new_observations(postavka):  # noqa: F811
    line = issued_line(postavka)
    event = event_for(postavka, new="90.05")
    original = detect(event)[0]
    reasons = deepcopy(original.unresolved_reasons)
    first_hash = original.last_observation_fingerprint
    InvoiceLine.objects.filter(pk=line.pk).update(
        osnova=Decimal("110.05"),
        ddv_znesek=Decimal("24.21"),
        skupaj=Decimal("134.26"),
    )
    changed = detect(event)[0]
    assert changed.unresolved_reasons[: len(reasons)] == reasons
    negatives = [
        reason
        for reason in changed.unresolved_reasons
        if reason["code"] == "NEGATIVE_ADJUSTMENT"
    ]
    assert [reason["amount"] for reason in negatives] == ["-10.00", "-20.00"]
    assert changed.znesek_poracuna == Decimal("-10.00")
    assert changed.evidence_state == original.evidence_state == "MANUAL_REVIEW"
    warnings = unresolved_warnings(line.invoice.company_id)
    assert "-10.00 EUR" in warnings[0] and "-20.00 EUR" in warnings[1]
    audit = audits(changed).last()
    assert audit.polje == "last_observation_fingerprint"
    assert audit.stara_vrednost == first_hash
    assert audit.nova_vrednost == changed.last_observation_fingerprint
    payload = json.loads(audit.razlog)
    assert payload["event"] == "INDEXATION_EVIDENCE_OBSERVATION"
    assert "evidence_state" not in payload["changes"]
    assert payload["changes"]["unresolved_reasons"] == {
        "old": reasons,
        "new": changed.unresolved_reasons,
    }
    assert payload["observation"]["calculation"]["adjustment_net"] == "-20.00"
    history = list(audits(changed).values())
    retry = detect(event)[0]
    assert retry.unresolved_reasons == changed.unresolved_reasons
    assert history == list(audits(changed).values())


def test_state_transition_audit_records_real_values(postavka):  # noqa: F811
    line = issued_line(postavka)
    event = event_for(postavka)
    original = detect(event)[0]
    InvoiceLine.objects.filter(pk=line.pk).update(
        razlog_rocnega_popravka="Preveriti popravek"
    )
    changed = detect(event)[0]
    audit = audits(changed).last()
    payload = json.loads(audit.razlog)
    assert payload["changes"]["evidence_state"] == {
        "old": "VERIFIED",
        "new": "UNRESOLVED",
    }
    assert audit.polje == "last_observation_fingerprint"
    assert audit.stara_vrednost == original.last_observation_fingerprint
    assert audit.nova_vrednost == changed.last_observation_fingerprint
    assert payload["reasons"] == changed.unresolved_reasons


def test_changed_observation_audit_failure_rolls_back_all_evidence(
    postavka,  # noqa: F811
    monkeypatch,
):
    line = issued_line(postavka)
    event = event_for(postavka, new="90.05")
    row = detect(event)[0]
    before = PoracunIndeksacije.objects.values().get(pk=row.pk)
    history = list(audits(row).values())
    InvoiceLine.objects.filter(pk=line.pk).update(
        razlog_rocnega_popravka="Preveriti popravek"
    )
    with monkeypatch.context() as context:

        def fail(*args, **kwargs):
            raise RuntimeError("audit failure")

        context.setattr(AuditLogEntry, "save", fail)
        with pytest.raises(RuntimeError, match="audit failure"):
            detect(event)
    assert PoracunIndeksacije.objects.values().get(pk=row.pk) == before
    assert list(audits(row).values()) == history
    assert detect(event)[0].evidence_state == "MANUAL_REVIEW"
    assert audits(row).count() == 2


@pytest.mark.parametrize(
    "invalid",
    ["consumption", "identity", "evidence", "snapshot_identity", "valid_source"],
)
def test_bulk_create_cannot_bypass_source_evidence_or_consumption_guards(
    postavka,  # noqa: F811
    invalid,
):
    line = issued_line(postavka)
    row = prepare_adjustments(event_for(postavka), business_month=date(2026, 3, 1))[0]
    if invalid == "consumption":
        row.vkljucen_v_racun = line.invoice
    elif invalid == "identity":
        row.original_invoice = issued_line(postavka, month=3).invoice
    elif invalid == "evidence":
        row.calculation_evidence = {}
    elif invalid == "snapshot_identity":
        row.calculation_evidence["source_line"]["id"] += 100
    with pytest.raises(ValidationError):
        PoracunIndeksacije.objects.bulk_create([row])
    assert not PoracunIndeksacije.objects.exists()
    assert not AuditLogEntry.objects.exists()


@pytest.mark.parametrize(
    "options",
    [
        {"ignore_conflicts": True},
        {
            "update_conflicts": True,
            "update_fields": ["unresolved_reasons"],
            "unique_fields": ["id"],
        },
    ],
)
def test_bulk_conflict_options_cannot_overwrite_frozen_observations(postavka, options):  # noqa: F811
    line = issued_line(postavka)
    event = event_for(postavka, new="90.05")
    existing = detect(event)[0]
    before = PoracunIndeksacije.objects.values().get(pk=existing.pk)
    existing.unresolved_reasons[0]["amount"] = "-999.00"
    with pytest.raises(ValidationError):
        PoracunIndeksacije.objects.bulk_create([existing], **options)
    assert PoracunIndeksacije.objects.values().get(pk=existing.pk) == before
    assert "-10.00 EUR" in unresolved_warnings(line.invoice.company_id)[0]


def test_bulk_batch_validation_finishes_before_any_insert(postavka):  # noqa: F811
    line = issued_line(postavka)
    event = event_for(postavka)
    legacy = PoracunIndeksacije(
        indexation_event=event,
        original_invoice=line.invoice,
        prizadeto_obdobje_od=line.invoice.obdobje_od,
        prizadeto_obdobje_do=line.invoice.obdobje_do,
        znesek_poracuna=Decimal("77.77"),
    )
    sourced = prepare_adjustments(event, business_month=date(2026, 3, 1))[0]
    sourced.vkljucen_v_racun = line.invoice
    with pytest.raises(ValidationError):
        PoracunIndeksacije.objects.bulk_create([legacy, sourced])
    assert not PoracunIndeksacije.objects.exists()


def test_legacy_bulk_create_preserves_existing_values_and_decisions(postavka):  # noqa: F811
    line = issued_line(postavka)
    event = event_for(postavka)
    rows = [
        PoracunIndeksacije(
            indexation_event=event,
            original_invoice=line.invoice,
            prizadeto_obdobje_od=line.invoice.obdobje_od,
            prizadeto_obdobje_do=line.invoice.obdobje_do,
            znesek_poracuna=Decimal("-77.77"),
            status=status,
            razlog_zavrnitve="Obstoječi razlog" if status == "ZAVRNJEN" else "",
            vkljucen_v_racun=line.invoice if status == "POTRJEN" else None,
        )
        for status in ["PREDLAGAN", "POTRJEN", "ZAVRNJEN"]
    ]
    PoracunIndeksacije.objects.bulk_create(iter(rows))
    assert PoracunIndeksacije.objects.count() == 3
    for row in PoracunIndeksacije.objects.all():
        assert row.znesek_poracuna == Decimal("-77.77")
        assert row.evidence_state == "LEGACY_UNVERIFIED"
        assert row.source_line_id is None and row.calculation_evidence == {}
    assert not AuditLogEntry.objects.exists()


def test_sourced_save_creation_is_audited_and_atomic(postavka, monkeypatch):  # noqa: F811
    issued_line(postavka)
    row = prepare_adjustments(event_for(postavka), business_month=date(2026, 3, 1))[0]
    with monkeypatch.context() as context:

        def fail(*args, **kwargs):
            raise RuntimeError("audit failure")

        context.setattr(AuditLogEntry, "save", fail)
        with pytest.raises(RuntimeError, match="audit failure"):
            row.save()
    assert not PoracunIndeksacije.objects.exists()
    row.pk = None
    row._state.adding = True
    row.save()
    assert audits(row).count() == 1
    assert audits(row).get().stara_vrednost == ""
    assert audits(row).get().nova_vrednost == row.last_observation_fingerprint


@pytest.mark.parametrize("field", ["source_identity", "fingerprint"])
def test_ordinary_sourced_insert_validates_identity_and_fingerprint(postavka, field):  # noqa: F811
    issued_line(postavka)
    row = prepare_adjustments(event_for(postavka), business_month=date(2026, 3, 1))[0]
    if field == "source_identity":
        row.calculation_evidence["source_line"]["id"] += 100
    else:
        row.last_observation_fingerprint = "0" * 64
    with pytest.raises(ValidationError):
        row.save()
    assert not PoracunIndeksacije.objects.exists()
    assert not AuditLogEntry.objects.exists()
