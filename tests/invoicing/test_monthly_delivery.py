# ruff: noqa: F401, F811
import pytest
from django.core.exceptions import ValidationError
from django.db import connection

from companies.services import potrdi_ddv_nastavitve, potrdi_vzorec_podjetja
from contracts.models import RecipientContact
from delivery.credentials import KeyringCredentialRef
from delivery.models import DeliveryAttempt
from delivery.services import (
    mark_accepted,
    mark_confirmed_failure,
    mark_result_unknown,
    mark_submission_started,
    resolve_unknown_as_sent,
)
from delivery.transport import SmtpConfig, SmtpSecurity
from invoicing.models import Invoice, MonthlyCandidateReview, MonthlyCycle
from invoicing.services import (
    NeveljavenPrehodMesecnegaCikla,
    PosiljanjeMesecnegaRacuna,
    StanjeMesecneDostave,
    izdaj_zamrznjeni_mesecni_paket,
    nastavi_izbiro_kandidata,
    odpri_zakljucek_mesecnega_cikla,
    poslji_dostavo_mesecnega_cikla,
    pripravi_dostavo_mesecnega_cikla,
    stanje_dostave_mesecnega_cikla,
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

pytestmark = pytest.mark.django_db(transaction=True)


def _open_step_6(data, settings, tmp_path):
    settings.RENDERING_ROOT = tmp_path / "rendered"
    izdaj_zamrznjeni_mesecni_paket(data["cycle"])
    renderiraj_koncne_pdfje_mesecnega_paketa(data["cycle"])
    data["cycle"] = odpri_zakljucek_mesecnega_cikla(data["cycle"])
    return list(Invoice.objects.order_by("lease_id"))


def _ready_step_6(data, settings, tmp_path):
    RecipientContact.objects.create(
        lease=data["lease"],
        email="recipient@example.com",
        vloga=RecipientContact.Vloga.TO,
        kanal=RecipientContact.Kanal.PDF,
    )
    _freeze_ready_package(data)
    return _open_step_6(data, settings, tmp_path)[0]


def _state(summary):
    return summary["per_invoice"][0]["delivery_state"]


def _send_input(invoice_id, *, subject="Invoice", body="Attached."):
    return PosiljanjeMesecnegaRacuna(
        invoice_id=invoice_id,
        smtp_config=SmtpConfig(
            host="smtp.example.com",
            port=587,
            security=SmtpSecurity.STARTTLS,
            timeout_seconds=10,
            username="smtp-user",
        ),
        credential_ref=KeyringCredentialRef("marjan", "smtp-user"),
        subject=subject,
        body=body,
    )


def test_first_preparation_is_idempotent_and_keeps_cycle_open(
    ready_company, settings, tmp_path, monkeypatch
):
    data = ready_company
    invoice = _ready_step_6(data, settings, tmp_path)
    monkeypatch.setattr(
        "delivery.services.send_pdf_email",
        lambda *args, **kwargs: pytest.fail("SMTP send must not run"),
    )
    before = stanje_dostave_mesecnega_cikla(data["cycle"])
    assert _state(before) == StanjeMesecneDostave.DELIVERY_NOT_PREPARED
    assert not DeliveryAttempt.objects.exists()

    first = pripravi_dostavo_mesecnega_cikla(data["cycle"])
    second = pripravi_dostavo_mesecnega_cikla(data["cycle"])

    assert not first["conflict_count"], first["per_invoice"][0]["message"]
    attempt = DeliveryAttempt.objects.get(invoice=invoice)
    cycle = MonthlyCycle.objects.get(pk=data["cycle"].pk)
    invoice.refresh_from_db()
    assert (attempt.sequence, attempt.result) == (1, DeliveryAttempt.Result.PREPARED)
    assert (
        attempt.sender,
        attempt.recipients_to,
        attempt.recipients_cc,
        attempt.recipients_bcc,
        attempt.pdf_path,
        attempt.pdf_checksum,
    ) == (
        "racuni@example.com",
        ["recipient@example.com"],
        [],
        ["racunovodja@example.com"],
        invoice.rendered_pdf_path,
        invoice.checksum,
    )
    assert DeliveryAttempt.objects.count() == 1
    assert first["per_invoice"][0]["latest_attempt_id"] == attempt.pk
    assert second["per_invoice"][0]["latest_attempt_id"] == attempt.pk
    assert _state(second) == StanjeMesecneDostave.DELIVERY_PREPARED
    assert not second["complete"]
    assert invoice.status == Invoice.Status.IZBRAN_ZA_POSILJANJE
    assert (cycle.status, cycle.trenutni_korak) == (
        MonthlyCycle.Status.V_TEKU,
        MonthlyCycle.Korak.ZAKLJUCEK,
    )


def test_zero_selected_is_complete_and_unselected_is_untouched(
    ready_company, settings, tmp_path
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

    summary = pripravi_dostavo_mesecnega_cikla(data["cycle"])

    invoice.refresh_from_db()
    assert summary["complete"] and summary["selected_count"] == 0
    assert summary["per_invoice"] == []
    assert invoice.status == Invoice.Status.ARHIVIRAN_NI_IZDAN
    assert not invoice.stevilka and not invoice.rendered_pdf_path
    assert not DeliveryAttempt.objects.exists()


def test_unselected_corruption_is_conflict_without_attempt(
    ready_company, settings, tmp_path
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
    Invoice.objects.filter(pk=invoice.pk).update(stevilka="NP-CORRUPT")

    summary = pripravi_dostavo_mesecnega_cikla(data["cycle"])

    assert not summary["complete"] and summary["conflict_count"] == 1
    assert _state(summary) == StanjeMesecneDostave.CONFLICT
    assert not DeliveryAttempt.objects.exists()


def test_sibling_preparation_failure_does_not_block_valid_invoice(
    ready_company, settings, tmp_path, monkeypatch
):
    data = ready_company
    second_lease = _add_lease(data, "P-B", with_item=True)
    potrdi_ddv_nastavitve(data["company"])
    potrdi_vzorec_podjetja(data["company"], data["lease"])
    for lease in (data["lease"], second_lease):
        for role in (RecipientContact.Vloga.TO, RecipientContact.Vloga.CC):
            RecipientContact.objects.create(
                lease=lease,
                email=f"lease-{lease.pk}-{role.lower()}@example.com",
                vloga=role,
                kanal=RecipientContact.Kanal.PDF,
            )
    _freeze_all_candidates(data)
    invoices = _open_step_6(data, settings, tmp_path)
    failed_id = invoices[0].pk
    from delivery import services as delivery_services

    real_prepare = delivery_services.prepare_attempt
    atomic_states = []

    def fail_one(invoice):
        atomic_states.append(connection.in_atomic_block)
        if invoice.pk == failed_id:
            raise ValidationError("Invalid sender")
        return real_prepare(invoice)

    monkeypatch.setattr(delivery_services, "prepare_attempt", fail_one)
    summary = pripravi_dostavo_mesecnega_cikla(data["cycle"])

    assert atomic_states == [False, False]
    assert DeliveryAttempt.objects.count() == 1
    assert DeliveryAttempt.objects.get().invoice_id == invoices[1].pk
    states = {item["invoice_id"]: item["delivery_state"] for item in summary["per_invoice"]}
    assert states[failed_id] == StanjeMesecneDostave.CONFLICT
    assert states[invoices[1].pk] == StanjeMesecneDostave.DELIVERY_PREPARED


@pytest.mark.parametrize(
    ("transition", "expected", "complete"),
    [
        ("prepared", StanjeMesecneDostave.DELIVERY_PREPARED, False),
        ("started", StanjeMesecneDostave.DELIVERY_IN_PROGRESS, False),
        ("accepted", StanjeMesecneDostave.DELIVERY_SENT, True),
        ("failed", StanjeMesecneDostave.DELIVERY_FAILED, False),
        ("unknown", StanjeMesecneDostave.DELIVERY_UNKNOWN, False),
        ("resolved", StanjeMesecneDostave.DELIVERY_SENT, True),
    ],
)
def test_read_states_and_prepare_never_retries(
    ready_company, settings, tmp_path, transition, expected, complete
):
    data = ready_company
    invoice = _ready_step_6(data, settings, tmp_path)
    prepared = pripravi_dostavo_mesecnega_cikla(data["cycle"])
    attempt = DeliveryAttempt.objects.get(pk=prepared["per_invoice"][0]["latest_attempt_id"])
    if transition != "prepared":
        attempt = mark_submission_started(attempt)
    if transition == "accepted":
        attempt = mark_accepted(attempt, code="SMTP_ACCEPTED")
    elif transition == "failed":
        attempt = mark_confirmed_failure(attempt, code="CONNECTION_FAILED")
    elif transition in {"unknown", "resolved"}:
        attempt = mark_result_unknown(attempt, code="SMTP_RESULT_UNKNOWN")
        if transition == "resolved":
            resolve_unknown_as_sent(attempt, reason="Verified in Sent folder")

    summary = pripravi_dostavo_mesecnega_cikla(data["cycle"])

    assert _state(summary) == expected
    assert summary["complete"] is complete
    assert DeliveryAttempt.objects.filter(invoice=invoice).count() == 1
    cycle = MonthlyCycle.objects.get(pk=data["cycle"].pk)
    assert (cycle.status, cycle.trenutni_korak) == (
        MonthlyCycle.Status.V_TEKU,
        MonthlyCycle.Korak.ZAKLJUCEK,
    )


def test_inconsistent_state_and_pdf_snapshot_mismatch_are_conflicts(
    ready_company, settings, tmp_path
):
    data = ready_company
    invoice = _ready_step_6(data, settings, tmp_path)
    pripravi_dostavo_mesecnega_cikla(data["cycle"])
    Invoice.objects.filter(pk=invoice.pk).update(status=Invoice.Status.POSLAN)
    assert _state(stanje_dostave_mesecnega_cikla(data["cycle"])) == StanjeMesecneDostave.CONFLICT

    Invoice.objects.filter(pk=invoice.pk).update(status=Invoice.Status.IZBRAN_ZA_POSILJANJE)
    DeliveryAttempt.objects.filter(invoice=invoice).update(pdf_checksum="0" * 64)
    assert _state(stanje_dostave_mesecnega_cikla(data["cycle"])) == StanjeMesecneDostave.CONFLICT


def test_unrelated_invoice_is_ignored_and_materialization_conflict_fails_closed(
    ready_company, settings, tmp_path
):
    data = ready_company
    invoice = _ready_step_6(data, settings, tmp_path)
    unrelated = Invoice.objects.create(
        company=data["company"],
        document_type=Invoice.DocumentType.NP,
        status=Invoice.Status.IZBRAN_ZA_POSILJANJE,
        stevilka="NP-99999",
        datum_izdaje=invoice.datum_izdaje,
        obdobje_od=invoice.obdobje_od,
        obdobje_do=invoice.obdobje_do,
    )
    Invoice.objects.filter(pk=invoice.pk).update(prejemnik_naziv="Corrupt snapshot")

    summary = pripravi_dostavo_mesecnega_cikla(data["cycle"])

    assert not summary["complete"]
    assert _state(summary) == StanjeMesecneDostave.CONFLICT
    assert not DeliveryAttempt.objects.exists()
    assert unrelated.pk not in {item["invoice_id"] for item in summary["per_invoice"]}


def test_wrong_step_rejects_before_preparation(ready_company, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "delivery.services.prepare_attempt", lambda *args: calls.append(args)
    )
    with pytest.raises(NeveljavenPrehodMesecnegaCikla, match="koraku 6"):
        pripravi_dostavo_mesecnega_cikla(ready_company["cycle"])
    assert not calls and not DeliveryAttempt.objects.exists()


def test_monthly_send_uses_persisted_result_without_outer_transaction(
    ready_company, settings, tmp_path, monkeypatch
):
    data = ready_company
    invoice = _ready_step_6(data, settings, tmp_path)
    pripravi_dostavo_mesecnega_cikla(data["cycle"])
    calls = []

    def send(attempt, **kwargs):
        calls.append((attempt.pk, connection.in_atomic_block, kwargs))
        started = mark_submission_started(attempt)
        return mark_accepted(started, code="SMTP_ACCEPTED", message="Accepted")

    monkeypatch.setattr("delivery.services.send_pdf_email", send)
    summary = poslji_dostavo_mesecnega_cikla(
        data["cycle"], [_send_input(invoice.pk)]
    )

    invoice.refresh_from_db()
    attempt = DeliveryAttempt.objects.get(invoice=invoice)
    cycle = MonthlyCycle.objects.get(pk=data["cycle"].pk)
    assert len(calls) == 1 and calls[0][1] is False
    assert invoice.status == Invoice.Status.POSLAN
    assert (attempt.result, attempt.result_code) == (
        DeliveryAttempt.Result.ACCEPTED,
        "SMTP_ACCEPTED",
    )
    assert summary["complete"] and summary["sent_count"] == 1
    assert _state(summary) == StanjeMesecneDostave.DELIVERY_SENT
    assert summary["per_invoice"][0]["latest_attempt_result_code"] == "SMTP_ACCEPTED"
    assert (cycle.status, cycle.trenutni_korak) == (
        MonthlyCycle.Status.V_TEKU,
        MonthlyCycle.Korak.ZAKLJUCEK,
    )


def test_monthly_send_isolates_siblings_and_never_retries_terminal_results(
    ready_company, settings, tmp_path, monkeypatch
):
    data = ready_company
    second_lease = _add_lease(data, "P-B", with_item=True)
    potrdi_ddv_nastavitve(data["company"])
    potrdi_vzorec_podjetja(data["company"], data["lease"])
    for lease in (data["lease"], second_lease):
        for role in (RecipientContact.Vloga.TO, RecipientContact.Vloga.CC):
            RecipientContact.objects.create(
                lease=lease,
                email=f"lease-{lease.pk}-{role.lower()}@example.com",
                vloga=role,
                kanal=RecipientContact.Kanal.PDF,
            )
    _freeze_all_candidates(data)
    invoices = _open_step_6(data, settings, tmp_path)
    pripravi_dostavo_mesecnega_cikla(data["cycle"])
    calls = []

    def send(attempt, **kwargs):
        calls.append(attempt.invoice_id)
        started = mark_submission_started(attempt)
        if attempt.invoice_id == invoices[0].pk:
            return mark_accepted(started, code="SMTP_ACCEPTED")
        return mark_confirmed_failure(started, code="SMTP_AUTH_FAILED")

    monkeypatch.setattr("delivery.services.send_pdf_email", send)
    inputs = [_send_input(invoice.pk) for invoice in invoices]
    first = poslji_dostavo_mesecnega_cikla(data["cycle"], inputs)
    second = poslji_dostavo_mesecnega_cikla(data["cycle"], inputs)

    states = {item["invoice_id"]: item["delivery_state"] for item in first["per_invoice"]}
    assert states == {
        invoices[0].pk: StanjeMesecneDostave.DELIVERY_SENT,
        invoices[1].pk: StanjeMesecneDostave.DELIVERY_FAILED,
    }
    assert (first["sent_count"], first["failed_count"]) == (1, 1)
    assert calls == [invoice.pk for invoice in invoices]
    assert second["sent_count"] == 1 and second["failed_count"] == 1


def test_monthly_send_missing_input_does_not_block_sibling(
    ready_company, settings, tmp_path, monkeypatch
):
    data = ready_company
    second_lease = _add_lease(data, "P-B", with_item=True)
    potrdi_ddv_nastavitve(data["company"])
    potrdi_vzorec_podjetja(data["company"], data["lease"])
    for lease in (data["lease"], second_lease):
        for role in (RecipientContact.Vloga.TO, RecipientContact.Vloga.CC):
            RecipientContact.objects.create(
                lease=lease,
                email=f"lease-{lease.pk}-{role.lower()}@example.com",
                vloga=role,
                kanal=RecipientContact.Kanal.PDF,
            )
    _freeze_all_candidates(data)
    invoices = _open_step_6(data, settings, tmp_path)
    pripravi_dostavo_mesecnega_cikla(data["cycle"])
    calls = []

    def send(attempt, **kwargs):
        calls.append(attempt.invoice_id)
        return mark_accepted(mark_submission_started(attempt), code="SMTP_ACCEPTED")

    monkeypatch.setattr("delivery.services.send_pdf_email", send)
    summary = poslji_dostavo_mesecnega_cikla(
        data["cycle"],
        [_send_input(invoices[1].pk)],
    )

    states = {item["invoice_id"]: item for item in summary["per_invoice"]}
    assert calls == [invoices[1].pk]
    assert states[invoices[0].pk]["delivery_state"] == StanjeMesecneDostave.CONFLICT
    assert "Manjka" in states[invoices[0].pk]["message"]
    assert states[invoices[1].pk]["delivery_state"] == StanjeMesecneDostave.DELIVERY_SENT
    assert summary["input_error_count"] == 1
    assert DeliveryAttempt.objects.get(invoice=invoices[0]).result == DeliveryAttempt.Result.PREPARED


@pytest.mark.parametrize("invalid_kind", ["unrelated", "duplicate", "object"])
def test_monthly_send_rejects_invalid_input_set_before_any_send(
    ready_company, settings, tmp_path, monkeypatch, invalid_kind
):
    data = ready_company
    second_lease = _add_lease(data, "P-B", with_item=True)
    potrdi_ddv_nastavitve(data["company"])
    potrdi_vzorec_podjetja(data["company"], data["lease"])
    for lease in (data["lease"], second_lease):
        for role in (RecipientContact.Vloga.TO, RecipientContact.Vloga.CC):
            RecipientContact.objects.create(
                lease=lease,
                email=f"lease-{lease.pk}-{role.lower()}@example.com",
                vloga=role,
                kanal=RecipientContact.Kanal.PDF,
            )
    _freeze_all_candidates(data)
    invoices = _open_step_6(data, settings, tmp_path)
    pripravi_dostavo_mesecnega_cikla(data["cycle"])
    inputs = [_send_input(invoice.pk) for invoice in invoices]
    if invalid_kind == "unrelated":
        unrelated = Invoice.objects.create(
            company=data["company"],
            document_type=Invoice.DocumentType.NP,
            status=Invoice.Status.IZBRAN_ZA_POSILJANJE,
            stevilka="NP-99999",
            datum_izdaje=invoices[0].datum_izdaje,
            obdobje_od=invoices[0].obdobje_od,
            obdobje_do=invoices[0].obdobje_do,
        )
        inputs.append(_send_input(unrelated.pk))
    elif invalid_kind == "duplicate":
        inputs.append(_send_input(invoices[0].pk))
    else:
        inputs.append(object())
    calls = []
    monkeypatch.setattr(
        "delivery.services.send_pdf_email", lambda *args, **kwargs: calls.append(args)
    )

    with pytest.raises(ValidationError, match="Vhodni nabor za pošiljanje ni veljaven"):
        poslji_dostavo_mesecnega_cikla(data["cycle"], inputs)

    assert not calls
    assert set(
        DeliveryAttempt.objects.values_list("result", flat=True)
    ) == {DeliveryAttempt.Result.PREPARED}
    assert set(Invoice.objects.filter(pk__in=[invoice.pk for invoice in invoices]).values_list("status", flat=True)) == {
        Invoice.Status.IZBRAN_ZA_POSILJANJE
    }
    cycle = MonthlyCycle.objects.get(pk=data["cycle"].pk)
    assert (cycle.status, cycle.trenutni_korak) == (
        MonthlyCycle.Status.V_TEKU,
        MonthlyCycle.Korak.ZAKLJUCEK,
    )


def test_monthly_send_unknown_and_validation_failure_continue_and_are_not_retried(
    ready_company, settings, tmp_path, monkeypatch
):
    data = ready_company
    second_lease = _add_lease(data, "P-B", with_item=True)
    potrdi_ddv_nastavitve(data["company"])
    potrdi_vzorec_podjetja(data["company"], data["lease"])
    for lease in (data["lease"], second_lease):
        for role in (RecipientContact.Vloga.TO, RecipientContact.Vloga.CC):
            RecipientContact.objects.create(
                lease=lease,
                email=f"lease-{lease.pk}-{role.lower()}@example.com",
                vloga=role,
                kanal=RecipientContact.Kanal.PDF,
            )
    _freeze_all_candidates(data)
    invoices = _open_step_6(data, settings, tmp_path)
    pripravi_dostavo_mesecnega_cikla(data["cycle"])
    calls = []

    def send(attempt, **kwargs):
        calls.append(attempt.invoice_id)
        if attempt.invoice_id == invoices[0].pk:
            raise ValidationError("Subject must not contain line breaks.")
        return mark_result_unknown(
            mark_submission_started(attempt), code="SMTP_RESULT_UNKNOWN"
        )

    monkeypatch.setattr("delivery.services.send_pdf_email", send)
    inputs = [
        _send_input(invoices[0].pk, subject="bad\nsubject"),
        _send_input(invoices[1].pk),
    ]
    first = poslji_dostavo_mesecnega_cikla(data["cycle"], inputs)
    second = poslji_dostavo_mesecnega_cikla(data["cycle"], inputs)

    states = {item["invoice_id"]: item for item in first["per_invoice"]}
    assert calls == [invoices[0].pk, invoices[1].pk, invoices[0].pk]
    assert states[invoices[0].pk]["delivery_state"] == StanjeMesecneDostave.CONFLICT
    assert states[invoices[1].pk]["delivery_state"] == StanjeMesecneDostave.DELIVERY_UNKNOWN
    assert second["unknown_count"] == 1


def test_monthly_send_runtime_exception_uses_transitioned_persisted_state(
    ready_company, settings, tmp_path, monkeypatch
):
    data = ready_company
    invoice = _ready_step_6(data, settings, tmp_path)
    pripravi_dostavo_mesecnega_cikla(data["cycle"])
    calls = []

    def send(attempt, **kwargs):
        calls.append(attempt.pk)
        mark_submission_started(attempt)
        raise RuntimeError("sensitive/internal backend failure")

    monkeypatch.setattr("delivery.services.send_pdf_email", send)
    execution_inputs = [_send_input(invoice.pk)]

    first = poslji_dostavo_mesecnega_cikla(data["cycle"], execution_inputs)
    second = poslji_dostavo_mesecnega_cikla(data["cycle"], execution_inputs)

    attempt = DeliveryAttempt.objects.get(invoice=invoice)
    invoice.refresh_from_db()
    cycle = MonthlyCycle.objects.get(pk=data["cycle"].pk)
    assert calls == [attempt.pk]
    assert _state(first) == StanjeMesecneDostave.DELIVERY_IN_PROGRESS
    assert _state(second) == StanjeMesecneDostave.DELIVERY_IN_PROGRESS
    assert attempt.result == DeliveryAttempt.Result.SUBMISSION_STARTED
    assert invoice.status == Invoice.Status.IZBRAN_ZA_POSILJANJE
    assert "sensitive/internal backend failure" not in str(first)
    assert "sensitive/internal backend failure" not in str(second)
    assert (cycle.status, cycle.trenutni_korak) == (
        MonthlyCycle.Status.V_TEKU,
        MonthlyCycle.Korak.ZAKLJUCEK,
    )


def test_monthly_send_rejects_wrong_step_before_domain_interaction(
    ready_company, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        "delivery.services.send_pdf_email", lambda *args, **kwargs: calls.append(args)
    )
    with pytest.raises(NeveljavenPrehodMesecnegaCikla, match="koraku 6"):
        poslji_dostavo_mesecnega_cikla(ready_company["cycle"], [])
    assert not calls


def test_monthly_send_zero_selected_is_complete(ready_company, settings, tmp_path):
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
    _open_step_6(data, settings, tmp_path)

    summary = poslji_dostavo_mesecnega_cikla(data["cycle"], [])

    assert summary["complete"] and summary["selected_count"] == 0
    assert summary["per_invoice"] == []
