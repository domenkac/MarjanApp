"""Explicit negative authorization never replaces historical evidence."""

# ruff: noqa: F811 -- Imported pytest fixtures are injected by name.
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
from contracts.models import IndexationEvent
from core.models import AuditLogEntry
from invoicing.models import Invoice, InvoiceLine
from tests.contracts.test_adjustment_review import decide
from tests.contracts.test_adjustment_write_safety import audits
from tests.contracts.test_indeksacija import postavka  # noqa: F401
from tests.contracts.test_late_adjustments import detect, event_for, issued_line
from tests.financial_fixtures import save_financial_fixture

REASON = "Preverjena pogodba in zgodovinski obračun."


def negative(item, *, partial=False, net=None, new="90.05"):
    line = issued_line(
        item, net=net or ("50.03" if partial else "100.05"), proportional=partial
    )
    periods = (
        {
            line.pk: {
                "start": date(2026, 2, 15),
                "end": date(2026, 2, 28),
                "reference": "Pogodba",
            }
        }
        if partial
        else {}
    )
    return detect(event_for(item, new=new), verified_periods=periods)[0]


@pytest.mark.parametrize("partial,amount", [(False, "-10.00"), (True, "-5.00")])
def test_negative_authorization_preserves_evidence_and_audit(postavka, partial, amount):
    row = negative(postavka, partial=partial)
    original = deepcopy(row.calculation_evidence), deepcopy(row.unresolved_reasons)
    sources = list(Invoice.objects.values()), list(InvoiceLine.objects.values())
    token = adjustment_review_token(row)
    with pytest.raises(ValidationError):
        decide(row)  # Ordinary positive approval is never negative authorization.
    approved = decide(row, "authorize_negative", REASON, token)
    assert approved.status == "POTRJEN" and approved.evidence_state == "MANUAL_REVIEW"
    assert approved.znesek_poracuna == Decimal(amount)
    assert (approved.calculation_evidence, approved.unresolved_reasons) == original
    approved.full_clean()
    receipt = json.loads(audits(row).last().razlog)
    assert receipt["action"] == "authorize_negative" and receipt["reason"] == REASON
    assert receipt["review_token"] == token
    assert decide(row, "authorize_negative", REASON, token).pk == row.pk
    assert audits(row).count() == 2
    assert unresolved_warnings(row.original_invoice.company_id) == []
    assert (
        list(Invoice.objects.values()),
        list(InvoiceLine.objects.values()),
    ) == sources
    with pytest.raises(ValidationError):
        decide(approved, "reject", REASON)
    InvoiceLine.objects.filter(pk=row.source_line_id).update(opis="Spremenjen vir")
    assert unresolved_warnings(row.original_invoice.company_id)


@pytest.mark.parametrize("reason", ["", " ", "ok", "..........", "x " * 10, None])
def test_negative_authorization_requires_meaningful_reason(postavka, reason):
    row = negative(postavka)
    with pytest.raises(ValidationError):
        decide(row, "authorize_negative", reason)
    row.refresh_from_db()
    assert row.status == "PREDLAGAN" and audits(row).count() == 1


@pytest.mark.parametrize(
    "problem", ["net", "period", "source", "rejected", "stale", "company", "positive"]
)
def test_negative_authorization_cannot_bypass_other_blocks(postavka, problem):
    row = negative(
        postavka,
        net="99.00" if problem == "net" else None,
        new="110.06" if problem == "positive" else "90.05",
    )
    token = adjustment_review_token(row)
    if problem == "period":
        # Detection cannot infer historical coverage from today's item dates.
        line = issued_line(postavka, month=3, proportional=True)
        row = detect(row.indexation_event)[1]
        assert row.source_line_id == line.pk
        token = adjustment_review_token(row)
    elif problem == "source":
        InvoiceLine.objects.filter(pk=row.source_line_id).update(opis="Novo dokazilo")
    elif problem == "rejected":
        row = decide(row, "reject", REASON)
        token = adjustment_review_token(row)
    elif problem == "stale":
        token = "stale"
    with pytest.raises((ValidationError, type(row).DoesNotExist)):
        decide(
            row,
            "authorize_negative",
            REASON,
            token,
            company_id=99999 if problem == "company" else None,
        )
    row.refresh_from_db()
    assert row.status != "POTRJEN"


def test_negative_audit_failure_rolls_back(postavka, monkeypatch):
    row = negative(postavka)

    def fail(*args, **kwargs):
        raise RuntimeError("audit failed")

    monkeypatch.setattr(AuditLogEntry, "save", fail)
    with pytest.raises(RuntimeError, match="audit failed"):
        decide(row, "authorize_negative", REASON)
    row.refresh_from_db()
    assert row.status == "PREDLAGAN" and audits(row).count() == 1


@pytest.mark.parametrize(
    "problem,code",
    [
        ("company", "COMPANY_OR_LEASE_MISMATCH"),
        ("lease", "COMPANY_OR_LEASE_MISMATCH"),
        ("duplicate", "AMBIGUOUS_SOURCE"),
        ("correction", "CORRECTION_HISTORY"),
        ("event", "EVENT_UNVERIFIED"),
        ("vat", "VAT_UNVERIFIED"),
        ("history", "RENT_HISTORY_UNVERIFIED"),
    ],
)
def test_negative_warning_never_overrides_other_evidence(postavka, problem, code):
    line = issued_line(postavka)
    event = event_for(postavka, new="90.05")
    if problem == "company":
        other = Company.objects.create(naziv="Drugo", davcna_stevilka="12345678")
        Invoice.objects.filter(pk=line.invoice_id).update(company=other)
    elif problem == "lease":
        Invoice.objects.filter(pk=line.invoice_id).update(lease=None)
    elif problem == "duplicate":
        issued_line(postavka)
    elif problem == "correction":
        Invoice.objects.create(
            company=line.invoice.company,
            lease=postavka.lease,
            document_type="DOBROPIS",
            povezan_racun=line.invoice,
            datum_izdaje=date(2026, 3, 1),
            obdobje_od=date(2026, 2, 1),
            obdobje_do=date(2026, 2, 28),
        )
    elif problem == "event":
        IndexationEvent.objects.create(
            lease_item=postavka,
            datum_uskladitve=date(2026, 2, 15),
            stara_vrednost=Decimal("90.05"),
            nova_vrednost=Decimal("80.00"),
            indeks_vrednost=Decimal("90.000"),
        )
    elif problem == "vat":
        postavka.vat_regime.stopnja = Decimal("9.50")
        save_financial_fixture(postavka.vat_regime)
    else:
        postavka.referencna_najemnina = None
        save_financial_fixture(postavka)
    row = detect(event)[0]
    assert {"NEGATIVE_ADJUSTMENT", code} <= {r["code"] for r in row.unresolved_reasons}
    with pytest.raises((ValidationError, type(row).DoesNotExist)):
        decide(
            row,
            "authorize_negative",
            REASON,
            company_id=postavka.lease.unit.property.company_id,
        )
    url = reverse(
        "ui:adjustment_detail", args=[postavka.lease.unit.property.company_id, row.pk]
    )
    page = Client().get(url)
    assert 'value="authorize_negative"' not in page.content.decode()
    row.refresh_from_db()
    assert row.status == "PREDLAGAN" and audits(row).count() == 1


def test_negative_ui_explicit_action_duplicate_post_and_csrf(postavka):
    row = negative(postavka)
    url = reverse(
        "ui:adjustment_detail", args=[row.original_invoice.company_id, row.pk]
    )
    client = Client()
    page = client.get(url)
    assert 'value="authorize_negative"' in page.content.decode()
    assert "-10.00" in page.content.decode() and "100.05" in page.content.decode()
    data = {
        "action": "authorize_negative",
        "reason": REASON,
        "token": page.context["token"],
    }
    assert Client(enforce_csrf_checks=True).post(url, data).status_code == 403
    assert client.post(url, data).status_code == 302
    assert client.post(url, data).status_code == 302
    assert REASON in Client().get(url).content.decode()
    assert audits(row).count() == 2


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("other", ["authorize_negative", "reject"])
def test_concurrent_negative_decisions_have_one_receipt(postavka, other):
    row = negative(postavka)
    token = adjustment_review_token(row)
    barrier = Barrier(2)
    company_id = row.original_invoice.company_id

    def run(action):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            try:
                return decide_adjustment(
                    company_id, row.pk, token, action, reason=REASON
                ).status
            except ValidationError:
                return "conflict"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ["authorize_negative", other]))
    assert audits(row).count() == 2
    row.refresh_from_db()
    assert (
        results == ["POTRJEN", "POTRJEN"]
        if other == "authorize_negative"
        else sorted(results) == sorted([row.status, "conflict"])
    )
