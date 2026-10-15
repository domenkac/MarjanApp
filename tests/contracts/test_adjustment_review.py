import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date
from decimal import Decimal
from threading import Barrier

import pytest
from django.core.exceptions import ValidationError
from django.db import close_old_connections
from django.test import Client
from django.urls import reverse

from companies.models import Company
from contracts.adjustments import (
    adjustment_review_token,
    decide_adjustment,
    unresolved_warnings,
)
from contracts.models import Lease, LeaseItem, PoracunIndeksacije, Property, Unit
from core.models import AuditLogEntry
from invoicing.models import Invoice, InvoiceLine
from tests.contracts.test_adjustment_write_safety import audits
from tests.contracts.test_indeksacija import postavka  # noqa: F401
from tests.contracts.test_late_adjustments import detect, event_for, issued_line
from tests.financial_fixtures import save_financial_fixture


def decide(row, action="approve", reason="", token=None, company_id=None):
    return decide_adjustment(
        company_id or row.original_invoice.company_id,
        row.pk,
        token if token is not None else adjustment_review_token(row),
        action,
        reason=reason,
    )


@pytest.mark.parametrize(
    "action,reason,status",
    [
        ("approve", "", "POTRJEN"),
        ("reject", "Dogovor", "ZAVRNJEN"),
    ],
)
def test_decision_audit_retry_conflict_and_resume(postavka, action, reason, status):  # noqa: F811
    issued_line(postavka)
    row = detect(event_for(postavka))[0]
    evidence = deepcopy(row.calculation_evidence)
    token = adjustment_review_token(row)
    decided = decide(row, action, reason, token)
    assert decided.status == status
    assert decided.razlog_zavrnitve == reason
    assert decided.calculation_evidence == evidence
    audit = audits(row).last()
    assert audit.polje == "status"
    assert audit.stara_vrednost == "PREDLAGAN" and audit.nova_vrednost == status
    payload = json.loads(audit.razlog)
    assert payload["reason"] == reason
    assert payload["changes"]["razlog_zavrnitve"] == {"old": "", "new": reason}
    assert audit.created_at is not None
    assert decide(row, action, reason, token).status == status
    assert audits(row).count() == 2
    with pytest.raises(ValidationError):
        decide(decided, "reject" if action == "approve" else "approve", "Drug razlog")
    if action == "reject":
        with pytest.raises(ValidationError):
            decide(decided, "reject", "Drug razlog", token)
    response = Client().get(
        reverse("ui:adjustment_detail", args=[row.original_invoice.company_id, row.pk])
    )
    assert response.status_code == 200
    assert response.context["row"].status == status
    assert response.context["decision_audit"].created_at == audit.created_at
    assert reason in response.content.decode()


@pytest.mark.parametrize("reason", ["", "   ", "\n\t"])
def test_rejection_requires_reason(postavka, reason):  # noqa: F811
    issued_line(postavka)
    row = detect(event_for(postavka))[0]
    with pytest.raises(ValidationError, match="Razlog"):
        decide(row, "reject", reason)
    row.refresh_from_db()
    assert row.status == "PREDLAGAN" and audits(row).count() == 1


@pytest.mark.parametrize("kind", ["negative", "unresolved", "legacy", "zero"])
def test_blocked_approval_rejection_preserves_warnings(postavka, kind):  # noqa: F811
    line = issued_line(postavka, proportional=kind == "unresolved")
    event = event_for(
        postavka,
        new="90.05" if kind == "negative" else "100.05" if kind == "zero" else "110.06",
    )
    if kind == "legacy":
        row = PoracunIndeksacije.objects.create(
            indexation_event=event,
            original_invoice=line.invoice,
            prizadeto_obdobje_od=date(2026, 2, 1),
            prizadeto_obdobje_do=date(2026, 2, 28),
            znesek_poracuna=Decimal("10.01"),
        )
    else:
        row = detect(event)[0]
    before = (
        deepcopy(row.calculation_evidence),
        deepcopy(row.unresolved_reasons),
        row.evidence_state,
    )
    warnings = unresolved_warnings(line.invoice.company_id)
    with pytest.raises(ValidationError):
        decide(row)
    rejected = decide(row, "reject", "Ročni pregled")
    assert (
        rejected.calculation_evidence,
        rejected.unresolved_reasons,
        rejected.evidence_state,
    ) == before
    assert unresolved_warnings(line.invoice.company_id) == warnings
    page = Client().get(
        reverse("ui:adjustment_detail", args=[line.invoice.company_id, row.pk])
    )
    assert page.context["blockers"]
    assert 'value="approve"' not in page.content.decode()
    if kind == "negative":
        assert "-10.00" in page.content.decode() and warnings


@pytest.mark.parametrize(
    "change", ["source", "observation", "event", "vat", "correction"]
)
def test_stale_evidence_cannot_be_approved(postavka, change):  # noqa: F811
    line = issued_line(postavka)
    event = event_for(postavka)
    row = detect(event)[0]
    token = adjustment_review_token(row)
    if change in {"source", "observation"}:
        InvoiceLine.objects.filter(pk=line.pk).update(
            razlog_rocnega_popravka="Spremenjeno"
        )
        if change == "observation":
            detect(event)
    elif change == "event":
        event.izkljucen = True
        event.save()
    elif change == "vat":
        regime = postavka.vat_regime
        regime.klavzula = "Spremenjeno"
        save_financial_fixture(regime)
    else:
        issued_line(postavka)
    with pytest.raises(ValidationError):
        decide(row, token=token)
    row.refresh_from_db()
    assert row.status == "PREDLAGAN"


@pytest.mark.parametrize("action,reason", [("approve", ""), ("reject", "Dogovor")])
def test_audit_failure_rolls_back_decision(postavka, monkeypatch, action, reason):  # noqa: F811
    issued_line(postavka)
    row = detect(event_for(postavka))[0]

    def fail(*args, **kwargs):
        raise RuntimeError("audit failed")

    monkeypatch.setattr(AuditLogEntry, "save", fail)
    with pytest.raises(RuntimeError, match="audit failed"):
        decide(row, action, reason)
    row.refresh_from_db()
    assert row.status == "PREDLAGAN" and audits(row).count() == 1


@pytest.mark.parametrize("method", ["save", "update", "bulk_update"])
@pytest.mark.parametrize(
    "field,value", [("status", "POTRJEN"), ("razlog_zavrnitve", "Brez revizije")]
)
def test_decisions_require_domain_service(postavka, method, field, value):  # noqa: F811
    issued_line(postavka)
    row = detect(event_for(postavka))[0]
    setattr(row, field, value)
    with pytest.raises(ValidationError):
        if method == "save":
            row.save(update_fields=[field])
        elif method == "update":
            PoracunIndeksacije.objects.filter(pk=row.pk).update(**{field: value})
        else:
            PoracunIndeksacije.objects.bulk_update([row], [field])


def test_company_isolation_and_csrf(postavka):  # noqa: F811
    issued_line(postavka)
    row = detect(event_for(postavka))[0]
    other = Company.objects.create(naziv="Drugo", davcna_stevilka="12345678")
    with pytest.raises(PoracunIndeksacije.DoesNotExist):
        decide(row, company_id=other.pk)
    url = reverse("ui:adjustment_detail", args=[other.pk, row.pk])
    assert Client().get(url).status_code == 404
    assert (
        Client()
        .post(url, {"action": "approve", "token": adjustment_review_token(row)})
        .status_code
        == 404
    )
    url = reverse(
        "ui:adjustment_detail", args=[row.original_invoice.company_id, row.pk]
    )
    assert (
        Client(enforce_csrf_checks=True).post(url, {"action": "approve"}).status_code
        == 403
    )


def test_ui_filters_navigation_decisions_and_validation(postavka):  # noqa: F811
    line = issued_line(postavka)
    row = detect(event_for(postavka))[0]
    client = Client()
    listing = reverse("ui:adjustment_list")
    response = client.get(
        listing,
        {
            "company": line.invoice.company_id,
            "lease": postavka.lease_id,
            "evidence_state": "VERIFIED",
        },
    )
    assert response.status_code == 200 and len(response.context["groups"]) == 1
    assert client.get(listing, {"evidence_state": "UNRESOLVED"}).context["groups"] == []
    assert client.get(listing, {"company": "bad"}).context["groups"] == []
    detail = reverse("ui:adjustment_detail", args=[line.invoice.company_id, row.pk])
    response = client.get(detail)
    text = response.content.decode()
    assert (
        line.invoice.stevilka in text
        and "100.05" in text
        and "110.06" in text
        and "+10.01" in text
    )
    assert "22.00" in text and "DDV22" in text
    assert listing in client.get(reverse("ui:indexation_list")).content.decode()
    assert (
        client.post(
            detail,
            {"action": "reject", "token": adjustment_review_token(row), "reason": " "},
        ).status_code
        == 200
    )
    assert client.post(detail, {"action": "approve", "token": "bad"}).context["error"]
    response = client.post(
        detail,
        {
            "action": "reject",
            "token": adjustment_review_token(row),
            "reason": "  Dogovor  ",
        },
    )
    assert response.status_code == 302
    assert Client().get(detail).context["row"].razlog_zavrnitve == "Dogovor"


@pytest.mark.parametrize("company", ["bad", "²", "9" * 5000])
def test_invalid_company_filter_fails_closed(postavka, company):  # noqa: F811
    response = Client().get(reverse("ui:adjustment_list"), {"company": company})
    assert response.status_code == 200
    assert response.context["form"].errors and response.context["groups"] == []


@pytest.mark.parametrize(
    "action,status", [("approve", "POTRJEN"), ("reject", "ZAVRNJEN")]
)
def test_http_decision_duplicate_click_and_fresh_browser(postavka, action, status):  # noqa: F811
    issued_line(postavka)
    row = detect(event_for(postavka))[0]
    url = reverse(
        "ui:adjustment_detail", args=[row.original_invoice.company_id, row.pk]
    )
    client = Client()
    data = {"token": client.get(url).context["token"], "action": action}
    if action == "reject":
        data["reason"] = "Dogovor"
    assert client.post(url, data).status_code == 302
    assert client.post(url, data).status_code == 302
    assert Client().get(url).context["row"].status == status
    assert audits(row).count() == 2


def test_filters_keep_company_lease_and_evidence_groups_separate(postavka):  # noqa: F811
    issued_line(postavka)
    first = detect(event_for(postavka))[0]
    second_line = issued_line(postavka, month=3, proportional=True)
    unresolved = detect(first.indexation_event)[1]
    assert unresolved.source_line_id == second_line.pk
    other = Company.objects.create(naziv="Drugo", davcna_stevilka="12345678")
    unit = Unit.objects.create(
        property=Property.objects.create(
            company=other, naziv="Druga stavba", naslov="Trg 2"
        ),
        oznaka="Enota",
        tip_enote="PISARNA",
    )
    lease = Lease.objects.create(
        unit=unit,
        tenant=postavka.lease.tenant,
        oznaka_pogodbe="P-2",
        datum_zacetka=date(2025, 1, 1),
        rok_placila_dni=15,
    )
    item = LeaseItem.objects.create(
        lease=lease,
        vat_regime=postavka.vat_regime,
        znesek_neto_mesecno=Decimal("100.05"),
    )
    issued_line(item)
    second = detect(event_for(item))[0]
    listing = reverse("ui:adjustment_list")
    client = Client()
    assert len(client.get(listing).context["groups"]) == 3
    response = client.get(
        listing, {"company": other.pk, "lease": lease.pk, "evidence_state": "VERIFIED"}
    )
    assert [
        entry["row"].pk
        for group in response.context["groups"]
        for entry in group["entries"]
    ] == [second.pk]
    response = client.get(listing, {"company": other.pk, "lease": postavka.lease_id})
    assert response.context["form"].errors and response.context["groups"] == []
    assert decide(second).status == "POTRJEN"
    first.refresh_from_db()
    assert first.status == "PREDLAGAN"


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_source_change_invalidates_review_for_any_decision(postavka, action):  # noqa: F811
    line = issued_line(postavka)
    row = detect(event_for(postavka))[0]
    token = adjustment_review_token(row)
    InvoiceLine.objects.filter(pk=line.pk).update(
        razlog_rocnega_popravka="Novo dokazilo"
    )
    with pytest.raises(ValidationError, match="zastarel"):
        decide(row, action, "Dogovor", token)
    row.refresh_from_db()
    assert row.status == "PREDLAGAN" and audits(row).count() == 1


def test_verified_partial_period_can_be_approved(postavka):  # noqa: F811
    line = issued_line(postavka, net="50.03", proportional=True)
    event = event_for(postavka)
    row = detect(
        event,
        verified_periods={
            line.pk: {
                "start": date(2026, 2, 15),
                "end": date(2026, 2, 28),
                "reference": "Pogodba",
            }
        },
    )[0]
    assert row.evidence_state == "VERIFIED"
    assert decide(row).status == "POTRJEN"


def test_rejected_unresolved_case_keeps_later_observations_and_warnings(postavka):  # noqa: F811
    line = issued_line(postavka, proportional=True)
    event = event_for(postavka)
    row = detect(event)[0]
    rejected = decide(row, "reject", "Potrebna uskladitev")
    token = adjustment_review_token(rejected)
    InvoiceLine.objects.filter(pk=line.pk).update(
        razlog_rocnega_popravka="Novo dokazilo"
    )
    observed = detect(event)[0]
    assert (
        observed.status == "ZAVRNJEN"
        and observed.razlog_zavrnitve == "Potrebna uskladitev"
    )
    assert observed.calculation_evidence == row.calculation_evidence
    assert unresolved_warnings(line.invoice.company_id)
    with pytest.raises(ValidationError):
        decide(observed, "reject", "Potrebna uskladitev", token)


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_decision_never_changes_source_or_consumes_adjustment(postavka, action):  # noqa: F811
    line = issued_line(postavka)
    row = detect(event_for(postavka))[0]
    original = Invoice.objects.filter(pk=line.invoice_id).values().get()
    source = deepcopy(InvoiceLine.objects.filter(pk=line.pk).values().get())
    decide(row, action, "Dogovor")
    assert Invoice.objects.filter(pk=line.invoice_id).values().get() == original
    assert InvoiceLine.objects.filter(pk=line.pk).values().get() == source
    row.refresh_from_db()
    assert row.vkljucen_v_racun_id is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "actions", [("approve", "approve"), ("reject", "reject"), ("approve", "reject")]
)
def test_concurrent_decisions_are_atomic_and_idempotent(postavka, actions):  # noqa: F811
    issued_line(postavka)
    row = detect(event_for(postavka))[0]
    company_id = row.original_invoice.company_id
    token = adjustment_review_token(row)
    barrier = Barrier(2)

    def run(action):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            try:
                return decide_adjustment(
                    company_id,
                    row.pk,
                    token,
                    action,
                    reason="Dogovor" if action == "reject" else "",
                ).status
            except ValidationError:
                return "conflict"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(run, actions))
    row.refresh_from_db()
    assert audits(row).count() == 2
    if actions[0] == actions[1]:
        assert outcomes == [row.status, row.status]
    else:
        assert sorted(outcomes) == sorted([row.status, "conflict"])
