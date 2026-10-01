# ruff: noqa: F401, F811
from pathlib import Path

import pytest

from archive.models import ArchivedFile
from archive.services import ArchiveRoots
from delivery.models import DeliveryAttempt
from delivery.services import resolve_unknown_as_sent
from invoicing.models import Invoice, MonthlyCandidateReview, MonthlyCycle
from invoicing.services import (
    NeveljavenPrehodMesecnegaCikla,
    _candidate_fingerprint,
    arhiviraj_poslane_racune_mesecnega_cikla,
    izdaj_zamrznjeni_mesecni_paket,
    nastavi_izbiro_kandidata,
    odpri_zakljucek_mesecnega_cikla,
    stanje_zakljucne_kontrole_mesecnega_cikla,
    uskladi_preglede_mesecnega_cikla,
    zamrzni_paket_za_potrditev,
)
from rendering.services import renderiraj_koncne_pdfje_mesecnega_paketa
from tests.invoicing.test_preflight import (
    _add_ready_company,
    _calculate,
    _freeze_all_candidates,
    _freeze_ready_package,
    ready_company,
)

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def roots(tmp_path):
    return ArchiveRoots((tmp_path / "local").resolve(), (tmp_path / "icloud").resolve())


def _open_step_6(data, settings, tmp_path):
    settings.RENDERING_ROOT = tmp_path / "rendered"
    izdaj_zamrznjeni_mesecni_paket(data["cycle"])
    renderiraj_koncne_pdfje_mesecnega_paketa(data["cycle"])
    data["cycle"] = odpri_zakljucek_mesecnega_cikla(data["cycle"])
    return list(Invoice.objects.order_by("company_id", "lease_id"))


def _selected_step_6(data, settings, tmp_path):
    _freeze_ready_package(data)
    return _open_step_6(data, settings, tmp_path)[0]


def _attempt(invoice, result, *, code="", message=""):
    attempt = DeliveryAttempt.objects.create(
        invoice=invoice,
        sequence=1,
        sender="sender@example.com",
        recipients_to=["recipient@example.com"],
        pdf_path=invoice.rendered_pdf_path,
        pdf_checksum=invoice.checksum,
        result=result,
        result_code=code,
        message=message,
    )
    statuses = {
        DeliveryAttempt.Result.ACCEPTED: Invoice.Status.POSLAN,
        DeliveryAttempt.Result.CONFIRMED_FAILURE: Invoice.Status.NAPAKA,
        DeliveryAttempt.Result.UNKNOWN: Invoice.Status.REZULTAT_NI_ZNAN,
    }
    if result in statuses:
        Invoice.objects.filter(pk=invoice.pk).update(status=statuses[result])
        invoice.refresh_from_db()
    return attempt


def _archive_sent(data, invoice, roots):
    _attempt(invoice, DeliveryAttempt.Result.ACCEPTED)
    assert arhiviraj_poslane_racune_mesecnega_cikla(data["cycle"], roots)["complete"]


def test_all_success_is_readiness_only(ready_company, settings, tmp_path, roots):
    data = ready_company
    invoice = _selected_step_6(data, settings, tmp_path)
    _archive_sent(data, invoice, roots)

    result = stanje_zakljucne_kontrole_mesecnega_cikla(data["cycle"])

    cycle = MonthlyCycle.objects.get(pk=data["cycle"].pk)
    assert result["all_required_work_successful"]
    assert not result["requires_attention"]
    assert result["headline"] == "Mesečni postopek uspešno zaključen"
    assert (result["prepared_count"], result["sent_count"], result["archive_verified_count"]) == (1, 1, 1)
    assert (cycle.status, cycle.trenutni_korak) == (MonthlyCycle.Status.V_TEKU, MonthlyCycle.Korak.ZAKLJUCEK)


def test_delivery_failure_reports_safe_detail(ready_company, settings, tmp_path):
    data = ready_company
    invoice = _selected_step_6(data, settings, tmp_path)
    _attempt(invoice, DeliveryAttempt.Result.CONFIRMED_FAILURE, code="SMTP_REJECTED", message="Mailbox rejected")

    result = stanje_zakljucne_kontrole_mesecnega_cikla(data["cycle"])

    issue = next(item for item in result["issues"] if item["category"] == "DELIVERY_FAILED")
    assert result["failed_count"] == 1 and result["requires_attention"]
    assert result["headline"] == "Mesečni postopek zahteva pozornost"
    assert (issue["company_id"], issue["invoice_id"], issue["delivery_result_code"], issue["delivery_result_message"]) == (
        invoice.company_id, invoice.pk, "SMTP_REJECTED", "Mailbox rejected"
    )


@pytest.mark.parametrize(
    ("result_state", "invoice_status", "count_name", "category"),
    [
        (DeliveryAttempt.Result.UNKNOWN, Invoice.Status.REZULTAT_NI_ZNAN, "unknown_count", "DELIVERY_UNKNOWN"),
        (DeliveryAttempt.Result.PREPARED, Invoice.Status.IZBRAN_ZA_POSILJANJE, "delivery_pending_count", "DELIVERY_PREPARED"),
        (DeliveryAttempt.Result.SUBMISSION_STARTED, Invoice.Status.IZBRAN_ZA_POSILJANJE, "delivery_pending_count", "DELIVERY_IN_PROGRESS"),
    ],
)
def test_open_delivery_states_require_attention(
    ready_company, settings, tmp_path, result_state, invoice_status, count_name, category
):
    data = ready_company
    invoice = _selected_step_6(data, settings, tmp_path)
    _attempt(invoice, result_state)
    Invoice.objects.filter(pk=invoice.pk).update(status=invoice_status)

    result = stanje_zakljucne_kontrole_mesecnega_cikla(data["cycle"])

    assert result["requires_attention"] and result[count_name] == 1
    assert category in {item["category"] for item in result["issues"]}


def test_delivery_not_prepared_requires_attention(ready_company, settings, tmp_path):
    data = ready_company
    _selected_step_6(data, settings, tmp_path)
    result = stanje_zakljucne_kontrole_mesecnega_cikla(data["cycle"])
    assert result["delivery_pending_count"] == 1 and result["requires_attention"]
    assert result["issues"][0]["category"] == "DELIVERY_NOT_PREPARED"


def test_archive_not_started_requires_attention(ready_company, settings, tmp_path):
    data = ready_company
    invoice = _selected_step_6(data, settings, tmp_path)
    _attempt(invoice, DeliveryAttempt.Result.ACCEPTED)

    result = stanje_zakljucne_kontrole_mesecnega_cikla(data["cycle"])

    assert result["delivery_complete"] and not result["archive_complete"]
    assert result["requires_attention"]
    assert "ARCHIVE_NOT_STARTED" in {item["category"] for item in result["issues"]}


def test_live_archive_divergence_reopens_attention(ready_company, settings, tmp_path, roots):
    data = ready_company
    invoice = _selected_step_6(data, settings, tmp_path)
    _archive_sent(data, invoice, roots)
    Path(ArchivedFile.objects.get().icloud_path).unlink()

    result = stanje_zakljucne_kontrole_mesecnega_cikla(data["cycle"])

    assert result["requires_attention"] and not result["all_required_work_successful"]
    assert "ARCHIVE_DIVERGENCE" in {item["category"] for item in result["issues"]}


def test_intentionally_unissued_and_zero_selected_can_succeed(ready_company, settings, tmp_path):
    data = ready_company
    _calculate(data)
    data["cycle"].trenutni_korak = MonthlyCycle.Korak.PAKETNI_PREGLED
    data["cycle"].save(update_fields=["trenutni_korak"])
    uskladi_preglede_mesecnega_cikla(data["cycle"])
    review = MonthlyCandidateReview.objects.get()
    nastavi_izbiro_kandidata(
        data["cycle"], lease_id=review.lease_id,
        candidate_fingerprint=review.candidate_fingerprint,
        selected=False, non_issue_reason="User decision",
    )
    zamrzni_paket_za_potrditev(data["cycle"])
    _open_step_6(data, settings, tmp_path)

    result = stanje_zakljucne_kontrole_mesecnega_cikla(data["cycle"])

    assert result["all_required_work_successful"]
    assert result["prepared_count"] == 0 and result["intentionally_unissued_count"] == 1
    assert result["issues"] == []


def test_systemically_unissued_uses_materialized_reason(ready_company, settings, tmp_path):
    data = ready_company
    candidate = _calculate(data)[0]
    candidate.update(
        will_issue=False,
        non_issue_reason="Pogodba nima obračunljivih postavk v tem obdobju",
        lines=[],
        totals={"net": "0.00", "vat": "0.00", "total": "0.00"},
        exceptions=[],
    )
    snapshot = data["batch"].calculation_snapshot
    candidate.pop("candidate_fingerprint")
    candidate["candidate_fingerprint"] = _candidate_fingerprint(
        candidate,
        company=snapshot["company"],
        period_start=snapshot["period_start"],
        period_end=snapshot["period_end"],
        issue_date=snapshot["issue_date"],
    )
    data["batch"].save(update_fields=["calculation_snapshot"])
    assert candidate["will_issue"] is False and candidate["non_issue_reason"]
    data["cycle"].trenutni_korak = MonthlyCycle.Korak.PAKETNI_PREGLED
    data["cycle"].save(update_fields=["trenutni_korak"])
    uskladi_preglede_mesecnega_cikla(data["cycle"])
    review = MonthlyCandidateReview.objects.get()
    assert not review.selected and review.non_issue_reason == ""
    zamrzni_paket_za_potrditev(data["cycle"])
    invoice = _open_step_6(data, settings, tmp_path)[0]

    result = stanje_zakljucne_kontrole_mesecnega_cikla(data["cycle"])
    cycle = MonthlyCycle.objects.get(pk=data["cycle"].pk)

    assert invoice.status == Invoice.Status.ARHIVIRAN_NI_IZDAN
    assert invoice.razlog_arhiviranja == candidate["non_issue_reason"]
    assert result["intentionally_unissued_count"] == 1
    assert result["conflict_count"] == 0 and result["issues"] == []
    assert result["all_required_work_successful"]
    assert result["headline"] == "Mesečni postopek uspešno zaključen"
    assert (cycle.status, cycle.trenutni_korak) == (
        MonthlyCycle.Status.V_TEKU,
        MonthlyCycle.Korak.ZAKLJUCEK,
    )


def test_mixed_companies_are_deterministic(ready_company, settings, tmp_path, roots):
    data = ready_company
    _add_ready_company(data)
    _freeze_all_candidates(data)
    first, second = _open_step_6(data, settings, tmp_path)
    _attempt(first, DeliveryAttempt.Result.ACCEPTED)
    _attempt(second, DeliveryAttempt.Result.CONFIRMED_FAILURE, message="Rejected")
    arhiviraj_poslane_racune_mesecnega_cikla(data["cycle"], roots)

    result = stanje_zakljucne_kontrole_mesecnega_cikla(data["cycle"])

    assert [item["company_id"] for item in result["per_company"]] == sorted([first.company_id, second.company_id])
    assert [item["requires_attention"] for item in result["per_company"]] == [False, True]
    assert [item["sent_count"] for item in result["per_company"]] == [1, 0]
    assert [item["failed_count"] for item in result["per_company"]] == [0, 1]


def test_materialization_conflict_requires_attention(ready_company, settings, tmp_path):
    data = ready_company
    invoice = _selected_step_6(data, settings, tmp_path)
    Invoice.objects.filter(pk=invoice.pk).update(prejemnik_naziv="Corrupt snapshot")

    result = stanje_zakljucne_kontrole_mesecnega_cikla(data["cycle"])

    assert result["conflict_count"] > 0 and result["requires_attention"]
    assert "PACKAGE_CONFLICT" in {item["category"] for item in result["issues"]}


def test_unknown_resolved_as_sent_can_succeed(ready_company, settings, tmp_path, roots):
    data = ready_company
    invoice = _selected_step_6(data, settings, tmp_path)
    attempt = _attempt(invoice, DeliveryAttempt.Result.UNKNOWN)
    resolve_unknown_as_sent(attempt, reason="Verified in Sent folder")
    assert arhiviraj_poslane_racune_mesecnega_cikla(data["cycle"], roots)["complete"]

    result = stanje_zakljucne_kontrole_mesecnega_cikla(data["cycle"])

    assert result["all_required_work_successful"] and result["unknown_count"] == 0


def test_final_control_is_read_only_and_uses_no_network_or_keychain(
    ready_company, settings, tmp_path, roots, monkeypatch
):
    data = ready_company
    invoice = _selected_step_6(data, settings, tmp_path)
    _archive_sent(data, invoice, roots)
    monkeypatch.setattr("delivery.services.send_pdf_email", lambda *a, **k: pytest.fail("network call"))
    monkeypatch.setattr("keyring.get_password", lambda *a, **k: pytest.fail("keychain call"))
    before = {
        "cycle": list(MonthlyCycle.objects.values()),
        "invoices": list(Invoice.objects.values()),
        "attempts": list(DeliveryAttempt.objects.values()),
        "archives": list(ArchivedFile.objects.values()),
    }

    stanje_zakljucne_kontrole_mesecnega_cikla(data["cycle"])

    after = {
        "cycle": list(MonthlyCycle.objects.values()),
        "invoices": list(Invoice.objects.values()),
        "attempts": list(DeliveryAttempt.objects.values()),
        "archives": list(ArchivedFile.objects.values()),
    }
    assert after == before


def test_wrong_step_rejects(ready_company):
    with pytest.raises(NeveljavenPrehodMesecnegaCikla, match="koraku 6"):
        stanje_zakljucne_kontrole_mesecnega_cikla(ready_company["cycle"])
