# ruff: noqa: F401, F811
from pathlib import Path

import pytest
from django.db import connection

from archive.models import ArchivedFile
from archive.services import ArchiveRoots, ArchiveState
from companies.services import potrdi_ddv_nastavitve
from delivery.models import DeliveryAttempt
from delivery.services import resolve_unknown_as_sent
from invoicing.models import Invoice, MonthlyCandidateReview, MonthlyCycle
from invoicing.services import (
    NeveljavenPrehodMesecnegaCikla,
    StanjeMesecnegaArhiva,
    arhiviraj_poslane_racune_mesecnega_cikla,
    izdaj_zamrznjeni_mesecni_paket,
    nastavi_izbiro_kandidata,
    odpri_zakljucek_mesecnega_cikla,
    potrdi_izjemo_kandidata,
    stanje_arhiviranja_mesecnega_cikla,
    uskladi_preglede_mesecnega_cikla,
    zamrzni_paket_za_potrditev,
)
from rendering.services import renderiraj_koncne_pdfje_mesecnega_paketa
from tests.invoicing.test_preflight import (
    _add_lease,
    _calculate,
    _freeze_all_candidates,
    _freeze_ready_package,
    ready_company,
)
from tests.setup_helpers import approve_sample, prepare_initial_setup

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def roots(tmp_path):
    return ArchiveRoots((tmp_path / "local").resolve(), (tmp_path / "icloud").resolve())


def _open_step_6(data, settings, tmp_path):
    prepare_initial_setup(data["company"], data["lease"])
    settings.RENDERING_ROOT = tmp_path / "rendered"
    izdaj_zamrznjeni_mesecni_paket(data["cycle"])
    renderiraj_koncne_pdfje_mesecnega_paketa(data["cycle"])
    data["cycle"] = odpri_zakljucek_mesecnega_cikla(data["cycle"])
    return list(Invoice.objects.order_by("lease_id"))


def _delivery_attempt(invoice, result=DeliveryAttempt.Result.ACCEPTED):
    return DeliveryAttempt.objects.create(
        invoice=invoice,
        sequence=1,
        sender="sender@example.com",
        recipients_to=["recipient@example.com"],
        pdf_path=invoice.rendered_pdf_path,
        pdf_checksum=invoice.checksum,
        result=result,
    )


def _mark_sent(invoice):
    attempt = _delivery_attempt(invoice)
    Invoice.objects.filter(pk=invoice.pk).update(status=Invoice.Status.POSLAN)
    invoice.refresh_from_db()
    return attempt


def test_one_sent_invoice_archives_without_completing_cycle(
    ready_company, settings, tmp_path, roots
):
    data = ready_company
    _freeze_ready_package(data)
    invoice = _open_step_6(data, settings, tmp_path)[0]
    _mark_sent(invoice)

    summary = arhiviraj_poslane_racune_mesecnega_cikla(data["cycle"], roots)

    archived = ArchivedFile.objects.get(invoice=invoice)
    cycle = MonthlyCycle.objects.get(pk=data["cycle"].pk)
    assert summary["complete"]
    assert summary["selected_count"] == summary["verified_count"] == 1
    assert summary["per_invoice"][0]["archive_state"] == StanjeMesecnegaArhiva.ARCHIVE_VERIFIED
    assert Path(archived.local_path).exists() and Path(archived.icloud_path).exists()
    assert Invoice.objects.get(pk=invoice.pk).status == Invoice.Status.POSLAN
    assert cycle.status == MonthlyCycle.Status.V_TEKU
    assert cycle.trenutni_korak == MonthlyCycle.Korak.ZAKLJUCEK


@pytest.mark.parametrize(
    ("invoice_status", "archive_state"),
    [
        (Invoice.Status.IZBRAN_ZA_POSILJANJE, StanjeMesecnegaArhiva.WAITING_DELIVERY),
        (Invoice.Status.NAPAKA, StanjeMesecnegaArhiva.DELIVERY_FAILED),
        (Invoice.Status.REZULTAT_NI_ZNAN, StanjeMesecnegaArhiva.DELIVERY_UNKNOWN),
    ],
)
def test_delivery_state_blocks_archive(
    ready_company, settings, tmp_path, roots, monkeypatch, invoice_status, archive_state
):
    data = ready_company
    _freeze_ready_package(data)
    invoice = _open_step_6(data, settings, tmp_path)[0]
    Invoice.objects.filter(pk=invoice.pk).update(status=invoice_status)
    calls = []
    monkeypatch.setattr("archive.services.archive_pdf", lambda *args: calls.append(args))

    summary = arhiviraj_poslane_racune_mesecnega_cikla(data["cycle"], roots)

    assert not calls and not summary["complete"]
    assert summary["per_invoice"][0]["archive_state"] == archive_state


def test_unknown_resolved_as_sent_uses_archive_domain_rules(
    ready_company, settings, tmp_path, roots
):
    data = ready_company
    _freeze_ready_package(data)
    invoice = _open_step_6(data, settings, tmp_path)[0]
    unknown = _delivery_attempt(invoice, DeliveryAttempt.Result.UNKNOWN)
    Invoice.objects.filter(pk=invoice.pk).update(status=Invoice.Status.REZULTAT_NI_ZNAN)

    resolve_unknown_as_sent(unknown, reason="Verified in Sent folder")
    summary = arhiviraj_poslane_racune_mesecnega_cikla(data["cycle"], roots)

    assert summary["complete"]
    assert DeliveryAttempt.objects.get(pk=unknown.pk).result == DeliveryAttempt.Result.UNKNOWN


def test_partial_failure_continues_and_retry_reuses_archive(
    ready_company, settings, tmp_path, roots, monkeypatch
):
    data = ready_company
    _add_lease(data, "P-B", with_item=True)
    potrdi_ddv_nastavitve(data["company"])
    approve_sample(data["company"], data["lease"])
    _freeze_all_candidates(data)
    invoices = _open_step_6(data, settings, tmp_path)
    for invoice in invoices:
        _mark_sent(invoice)
    from archive import services as archive_services

    real_archive = archive_services.archive_pdf
    failed_id = invoices[0].pk

    def fail_one(invoice, archive_roots):
        if invoice.pk == failed_id:
            raise OSError("simulated failure")
        return real_archive(invoice, archive_roots)

    monkeypatch.setattr(archive_services, "archive_pdf", fail_one)
    first = arhiviraj_poslane_racune_mesecnega_cikla(data["cycle"], roots)
    good = ArchivedFile.objects.get(invoice=invoices[1])
    good_paths = (good.local_path, good.icloud_path)
    monkeypatch.setattr(archive_services, "archive_pdf", real_archive)
    second = arhiviraj_poslane_racune_mesecnega_cikla(data["cycle"], roots)

    assert not first["complete"] and second["complete"]
    assert ArchivedFile.objects.count() == 2
    good.refresh_from_db()
    assert (good.local_path, good.icloud_path) == good_paths


def test_live_divergence_reopens_archive_subtask(
    ready_company, settings, tmp_path, roots
):
    data = ready_company
    _freeze_ready_package(data)
    invoice = _open_step_6(data, settings, tmp_path)[0]
    _mark_sent(invoice)
    assert arhiviraj_poslane_racune_mesecnega_cikla(data["cycle"], roots)["complete"]
    archived = ArchivedFile.objects.get()
    Path(archived.icloud_path).unlink()

    summary = stanje_arhiviranja_mesecnega_cikla(data["cycle"])

    assert not summary["complete"]
    assert summary["per_invoice"][0]["archive_state"] == ArchiveState.ICLOUD_MISSING


def test_zero_selected_requires_no_archive_and_does_not_complete_cycle(
    ready_company, settings, tmp_path, roots, monkeypatch
):
    data = ready_company
    _calculate(data)
    data["cycle"].trenutni_korak = MonthlyCycle.Korak.PAKETNI_PREGLED
    data["cycle"].save(update_fields=["trenutni_korak"])
    uskladi_preglede_mesecnega_cikla(data["cycle"])
    review = MonthlyCandidateReview.objects.get()
    nastavi_izbiro_kandidata(
        data["cycle"],
        lease_id=review.lease_id,
        candidate_fingerprint=review.candidate_fingerprint,
        selected=False,
        non_issue_reason="User decision",
    )
    zamrzni_paket_za_potrditev(data["cycle"])
    invoice = _open_step_6(data, settings, tmp_path)[0]
    calls = []
    monkeypatch.setattr("archive.services.archive_pdf", lambda *args: calls.append(args))

    summary = arhiviraj_poslane_racune_mesecnega_cikla(data["cycle"], roots)

    invoice.refresh_from_db()
    assert summary["complete"] and summary["selected_count"] == 0
    assert not calls and not ArchivedFile.objects.exists()
    assert invoice.status == Invoice.Status.ARHIVIRAN_NI_IZDAN
    assert MonthlyCycle.objects.get(pk=data["cycle"].pk).status == MonthlyCycle.Status.V_TEKU


def test_unselected_sibling_is_untouched(ready_company, settings, tmp_path, roots):
    data = ready_company
    _add_lease(data, "P-B", with_item=True)
    potrdi_ddv_nastavitve(data["company"])
    approve_sample(data["company"], data["lease"])
    _calculate(data)
    data["cycle"].trenutni_korak = MonthlyCycle.Korak.PAKETNI_PREGLED
    data["cycle"].save(update_fields=["trenutni_korak"])
    uskladi_preglede_mesecnega_cikla(data["cycle"])
    reviews = list(MonthlyCandidateReview.objects.order_by("lease_id"))
    nastavi_izbiro_kandidata(
        data["cycle"], lease_id=reviews[1].lease_id,
        candidate_fingerprint=reviews[1].candidate_fingerprint,
        selected=False, non_issue_reason="User decision",
    )
    for review in MonthlyCandidateReview.objects.filter(selected=True):
        if review.requires_confirmation:
            potrdi_izjemo_kandidata(
                data["cycle"], lease_id=review.lease_id,
                candidate_fingerprint=review.candidate_fingerprint,
            )
    zamrzni_paket_za_potrditev(data["cycle"])
    invoices = _open_step_6(data, settings, tmp_path)
    selected = next(invoice for invoice in invoices if invoice.stevilka)
    unselected = next(invoice for invoice in invoices if not invoice.stevilka)
    _mark_sent(selected)

    summary = arhiviraj_poslane_racune_mesecnega_cikla(data["cycle"], roots)

    unselected.refresh_from_db()
    assert summary["complete"] and ArchivedFile.objects.count() == 1
    assert unselected.status == Invoice.Status.ARHIVIRAN_NI_IZDAN
    assert not ArchivedFile.objects.filter(invoice=unselected).exists()


def test_wrong_step_rejects_before_archive(ready_company, roots, monkeypatch):
    calls = []
    monkeypatch.setattr("archive.services.archive_pdf", lambda *args: calls.append(args))
    with pytest.raises(NeveljavenPrehodMesecnegaCikla, match="koraku 6"):
        arhiviraj_poslane_racune_mesecnega_cikla(ready_company["cycle"], roots)
    assert not calls


def test_only_frozen_cycle_members_are_archived(
    ready_company, settings, tmp_path, roots, monkeypatch
):
    data = ready_company
    _freeze_ready_package(data)
    invoice = _open_step_6(data, settings, tmp_path)[0]
    _mark_sent(invoice)
    unrelated = Invoice.objects.create(
        company=data["company"], document_type=Invoice.DocumentType.NP,
        status=Invoice.Status.POSLAN, stevilka="NP-99999",
        datum_izdaje=invoice.datum_izdaje, obdobje_od=invoice.obdobje_od,
        obdobje_do=invoice.obdobje_do,
    )
    from archive import services as archive_services
    real_archive = archive_services.archive_pdf
    called = []

    def record(candidate, archive_roots):
        called.append(candidate.pk)
        return real_archive(candidate, archive_roots)

    monkeypatch.setattr(archive_services, "archive_pdf", record)
    arhiviraj_poslane_racune_mesecnega_cikla(data["cycle"], roots)
    assert called == [invoice.pk] and unrelated.pk not in called


def test_archive_call_runs_outside_transaction(
    ready_company, settings, tmp_path, roots, monkeypatch
):
    data = ready_company
    _freeze_ready_package(data)
    invoice = _open_step_6(data, settings, tmp_path)[0]
    _mark_sent(invoice)
    from archive import services as archive_services
    real_archive = archive_services.archive_pdf
    atomic_states = []

    def checked(candidate, archive_roots):
        atomic_states.append(connection.in_atomic_block)
        return real_archive(candidate, archive_roots)

    monkeypatch.setattr(archive_services, "archive_pdf", checked)
    assert arhiviraj_poslane_racune_mesecnega_cikla(data["cycle"], roots)["complete"]
    assert atomic_states == [False]


def test_unselected_corruption_fails_closed(ready_company, settings, tmp_path, roots):
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
    invoice = _open_step_6(data, settings, tmp_path)[0]
    Invoice.objects.filter(pk=invoice.pk).update(stevilka="NP-CORRUPT")

    summary = stanje_arhiviranja_mesecnega_cikla(data["cycle"])

    assert not summary["complete"]
    assert summary["per_invoice"][0]["archive_state"] == StanjeMesecnegaArhiva.CONFLICT
    assert not ArchivedFile.objects.exists()


def test_materialization_conflict_never_substitutes_an_invoice(
    ready_company, settings, tmp_path, roots, monkeypatch
):
    data = ready_company
    _freeze_ready_package(data)
    invoice = _open_step_6(data, settings, tmp_path)[0]
    Invoice.objects.filter(pk=invoice.pk).update(prejemnik_naziv="Corrupt snapshot")
    calls = []
    monkeypatch.setattr("archive.services.archive_pdf", lambda *args: calls.append(args))

    summary = arhiviraj_poslane_racune_mesecnega_cikla(data["cycle"], roots)

    assert not summary["complete"] and summary["selected_count"] == 1
    assert summary["issue_count"] == 1 and not calls
    assert summary["per_invoice"][0]["archive_state"] == StanjeMesecnegaArhiva.CONFLICT
