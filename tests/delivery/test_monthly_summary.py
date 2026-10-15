# ruff: noqa: F401, F811

from copy import deepcopy

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from archive.models import ArchivedFile
from archive.services import ArchiveRoots
from contracts.models import RecipientContact
from delivery.configuration import (
    SmtpConfigurationError,
    resolve_monthly_summary_configuration,
    resolve_monthly_summary_credential_ref,
    resolve_monthly_summary_smtp_config,
)
from delivery.credentials import KeyringCredentialRef
from delivery.models import (
    DeliveryAttempt,
    MonthlySummaryAttempt,
    MonthlySummaryConfiguration,
)
from delivery.services import (
    prepare_monthly_summary,
    prepare_monthly_summary_retry,
    send_monthly_summary,
)
from delivery.transport import SmtpConfig, SmtpResult, SmtpSecurity
from invoicing.models import Invoice, MonthlyCandidateReview, MonthlyCycle
from invoicing.services import (
    NeveljavenPrehodMesecnegaCikla,
    arhiviraj_poslane_racune_mesecnega_cikla,
    izdaj_zamrznjeni_mesecni_paket,
    nastavi_izbiro_kandidata,
    odpri_zakljucek_mesecnega_cikla,
    uskladi_preglede_mesecnega_cikla,
    zamrzni_paket_za_potrditev,
)
from rendering.services import renderiraj_koncne_pdfje_mesecnega_paketa
from tests.business_fixtures import update_business_records
from tests.invoicing.test_preflight import (
    _calculate,
    _freeze_ready_package,
    ready_company,
)

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def configuration(settings):
    settings.MARJAN_SMTP_HOST = "smtp.example.com"
    settings.MARJAN_SMTP_PORT = 587
    settings.MARJAN_SMTP_SECURITY = "STARTTLS"
    settings.MARJAN_SMTP_TIMEOUT_SECONDS = 12.5
    settings.MARJAN_KEYRING_SERVICE = "marjan-smtp"
    return MonthlySummaryConfiguration.objects.create(
        email="owner@example.com",
        keychain_credential_key="monthly-summary",
    )


def _step_6(data, settings, tmp_path):
    settings.RENDERING_ROOT = tmp_path / "rendered"
    _freeze_ready_package(data)
    izdaj_zamrznjeni_mesecni_paket(data["cycle"])
    renderiraj_koncne_pdfje_mesecnega_paketa(data["cycle"])
    data["cycle"] = odpri_zakljucek_mesecnega_cikla(data["cycle"])
    return Invoice.objects.get()


def _attempt(invoice, result, *, message=""):
    attempt = DeliveryAttempt.objects.create(
        invoice=invoice,
        sequence=1,
        sender="company@example.com",
        recipients_to=["saved-to@example.com"],
        recipients_cc=["saved-cc@example.com"],
        recipients_bcc=["saved-bcc@example.com"],
        pdf_path=invoice.rendered_pdf_path,
        pdf_checksum=invoice.checksum,
        result=result,
        message=message,
    )
    status = {
        DeliveryAttempt.Result.ACCEPTED: Invoice.Status.POSLAN,
        DeliveryAttempt.Result.CONFIRMED_FAILURE: Invoice.Status.NAPAKA,
        DeliveryAttempt.Result.UNKNOWN: Invoice.Status.REZULTAT_NI_ZNAN,
    }.get(result)
    if status:
        Invoice.objects.filter(pk=invoice.pk).update(status=status)
    return attempt


def _success_attempt(data, settings, tmp_path):
    invoice = _step_6(data, settings, tmp_path)
    _attempt(invoice, DeliveryAttempt.Result.ACCEPTED)
    roots = ArchiveRoots((tmp_path / "local").resolve(), (tmp_path / "icloud").resolve())
    assert arhiviraj_poslane_racune_mesecnega_cikla(data["cycle"], roots)["complete"]
    return invoice


def test_configuration_is_singleton_and_stores_no_password(configuration):
    assert configuration.pk == 1
    assert {field.name for field in configuration._meta.fields} == {
        "id", "email", "keychain_credential_key", "created_at", "updated_at"
    }
    with pytest.raises(ValidationError, match="pk=1"):
        MonthlySummaryConfiguration(id=2, email="x@example.com", keychain_credential_key="x").save()
    with pytest.raises(IntegrityError), transaction.atomic():
        MonthlySummaryConfiguration.objects.bulk_create(
            [MonthlySummaryConfiguration(id=2, email="x@example.com", keychain_credential_key="x")]
        )


def test_summary_configuration_resolvers_use_same_email_and_key(configuration):
    assert resolve_monthly_summary_configuration() == configuration
    assert resolve_monthly_summary_smtp_config(configuration) == SmtpConfig(
        host="smtp.example.com", port=587, security=SmtpSecurity.STARTTLS,
        timeout_seconds=12.5, username="owner@example.com",
    )
    assert resolve_monthly_summary_credential_ref(configuration) == KeyringCredentialRef(
        "marjan-smtp", "monthly-summary"
    )


def test_missing_or_invalid_persisted_configuration_fails_closed(configuration):
    MonthlySummaryConfiguration.objects.filter(pk=1).update(email="")
    with pytest.raises(SmtpConfigurationError):
        resolve_monthly_summary_configuration()
    MonthlySummaryConfiguration.objects.filter(pk=1).delete()
    with pytest.raises(SmtpConfigurationError):
        resolve_monthly_summary_configuration()


def test_unsaved_or_dirty_configuration_is_rejected(ready_company, configuration):
    unsaved = MonthlySummaryConfiguration(
        email="other@example.com", keychain_credential_key="other"
    )
    assert unsaved.pk == 1 and unsaved._state.adding
    with pytest.raises(SmtpConfigurationError):
        prepare_monthly_summary(ready_company["cycle"], unsaved)

    configuration.email = "dirty@example.com"
    with pytest.raises(SmtpConfigurationError):
        prepare_monthly_summary(ready_company["cycle"], configuration)
    assert not MonthlySummaryAttempt.objects.exists()


def test_preparation_requires_step_6_and_rejects_pending(ready_company, configuration, settings, tmp_path):
    with pytest.raises(NeveljavenPrehodMesecnegaCikla, match="koraku 6"):
        prepare_monthly_summary(ready_company["cycle"], configuration)
    _step_6(ready_company, settings, tmp_path)
    with pytest.raises(ValidationError, match="pending"):
        prepare_monthly_summary(ready_company["cycle"], configuration)
    assert not MonthlySummaryAttempt.objects.exists()


def test_success_snapshot_body_and_idempotency(ready_company, configuration, settings, tmp_path):
    _success_attempt(ready_company, settings, tmp_path)
    attempt = prepare_monthly_summary(ready_company["cycle"], configuration)

    assert (attempt.sequence, attempt.result) == (1, MonthlySummaryAttempt.Result.PREPARED)
    assert attempt.sender == attempt.recipient == configuration.email
    period = ready_company["cycle"].obdobje
    assert attempt.subject == f"Mesečni postopek – {period:%m/%Y}"
    snapshot = attempt.summary_snapshot
    assert attempt.body.startswith(snapshot["headline"] + "\n")
    assert [(item["category"], item["message"]) for item in snapshot["issues"]] == []
    assert snapshot["headline"] == "Mesečni postopek uspešno zaključen", snapshot
    assert snapshot["period"] == period.isoformat() and snapshot["prepared_at"]
    assert snapshot["counts"] == {
        "prepared": 1, "sent": 1, "intentionally_unissued": 0,
        "failed": 0, "unknown": 0,
    }
    assert snapshot["archive"]["complete"] and snapshot["per_company"][0]["issuer_label"]
    with pytest.raises(ValidationError, match="already exists"):
        prepare_monthly_summary(ready_company["cycle"], configuration)
    assert MonthlySummaryAttempt.objects.count() == 1
    MonthlySummaryAttempt.objects.filter(pk=attempt.pk).update(
        result=MonthlySummaryAttempt.Result.ACCEPTED
    )
    with pytest.raises(ValidationError, match="already accepted"):
        prepare_monthly_summary(ready_company["cycle"], configuration)
    assert MonthlySummaryAttempt.objects.count() == 1


def test_attention_uses_persisted_attempt_recipients(ready_company, configuration, settings, tmp_path):
    invoice = _step_6(ready_company, settings, tmp_path)
    _attempt(invoice, DeliveryAttempt.Result.CONFIRMED_FAILURE, message="Mailbox rejected")
    update_business_records(RecipientContact.objects.filter(lease=invoice.lease), email="live@example.com")

    attempt = prepare_monthly_summary(ready_company["cycle"], configuration)

    assert attempt.summary_snapshot["headline"] == "Mesečni postopek zahteva pozornost"
    issue = next(item for item in attempt.summary_snapshot["issues"] if item["category"] == "DELIVERY_FAILED")
    assert issue["recipients"] == {
        "to": ["saved-to@example.com"],
        "cc": ["saved-cc@example.com"],
        "bcc": ["saved-bcc@example.com"],
    }
    assert "live@example.com" not in attempt.body and "Mailbox rejected" in attempt.body


def test_zero_selected_package_prepares_success_without_error(
    ready_company, configuration, settings, tmp_path
):
    _calculate(ready_company)
    cycle = ready_company["cycle"]
    cycle.trenutni_korak = MonthlyCycle.Korak.PAKETNI_PREGLED
    cycle.save(update_fields=["trenutni_korak"])
    uskladi_preglede_mesecnega_cikla(cycle)
    review = MonthlyCandidateReview.objects.get()
    nastavi_izbiro_kandidata(
        cycle,
        lease_id=review.lease_id,
        candidate_fingerprint=review.candidate_fingerprint,
        selected=False,
        non_issue_reason="User decision",
    )
    zamrzni_paket_za_potrditev(cycle)
    settings.RENDERING_ROOT = tmp_path / "rendered"
    izdaj_zamrznjeni_mesecni_paket(cycle)
    renderiraj_koncne_pdfje_mesecnega_paketa(cycle)
    cycle = odpri_zakljucek_mesecnega_cikla(cycle)

    attempt = prepare_monthly_summary(cycle, configuration)

    assert attempt.summary_snapshot["headline"] == "Mesečni postopek uspešno zaključen"
    assert attempt.summary_snapshot["counts"]["intentionally_unissued"] == 1
    assert attempt.summary_snapshot["issues"] == []


def test_retry_copies_exact_snapshot_and_unknown_is_blocked(ready_company, configuration, settings, tmp_path):
    _success_attempt(ready_company, settings, tmp_path)
    source = prepare_monthly_summary(ready_company["cycle"], configuration)
    MonthlySummaryAttempt.objects.filter(pk=source.pk).update(result=MonthlySummaryAttempt.Result.CONFIRMED_FAILURE)
    source.refresh_from_db()
    expected = (source.sender, source.recipient, source.subject, source.body, deepcopy(source.summary_snapshot))

    retry = prepare_monthly_summary_retry(source)

    assert retry.sequence == 2
    assert (retry.sender, retry.recipient, retry.subject, retry.body, retry.summary_snapshot) == expected
    MonthlySummaryAttempt.objects.filter(pk=retry.pk).update(result=MonthlySummaryAttempt.Result.UNKNOWN)
    retry.refresh_from_db()
    with pytest.raises(ValidationError, match="confirmed failure"):
        prepare_monthly_summary_retry(retry)


def test_cycle_must_remain_active_before_prepare_write(
    ready_company, configuration, settings, tmp_path, monkeypatch
):
    _success_attempt(ready_company, settings, tmp_path)
    from invoicing import services as invoicing_services

    authoritative = invoicing_services.stanje_zakljucne_kontrole_mesecnega_cikla

    def close_after_final_control(cycle):
        result = authoritative(cycle)
        MonthlyCycle.objects.filter(pk=cycle.pk).update(status=MonthlyCycle.Status.ZAKLJUCEN)
        return result

    monkeypatch.setattr(
        invoicing_services,
        "stanje_zakljucne_kontrole_mesecnega_cikla",
        close_after_final_control,
    )
    with pytest.raises(ValidationError, match="remain active"):
        prepare_monthly_summary(ready_company["cycle"], configuration)
    assert not MonthlySummaryAttempt.objects.exists()


def test_cycle_must_be_active_before_retry(
    ready_company, configuration, settings, tmp_path
):
    _success_attempt(ready_company, settings, tmp_path)
    source = prepare_monthly_summary(ready_company["cycle"], configuration)
    MonthlySummaryAttempt.objects.filter(pk=source.pk).update(
        result=MonthlySummaryAttempt.Result.CONFIRMED_FAILURE
    )
    MonthlyCycle.objects.filter(pk=ready_company["cycle"].pk).update(
        status=MonthlyCycle.Status.ZAKLJUCEN
    )
    source.refresh_from_db()
    with pytest.raises(ValidationError, match="remain active"):
        prepare_monthly_summary_retry(source)
    assert MonthlySummaryAttempt.objects.count() == 1


@pytest.mark.parametrize(
    ("smtp_result", "expected"),
    [
        (SmtpResult(True, "SMTP_ACCEPTED", submission_started=True), MonthlySummaryAttempt.Result.ACCEPTED),
        (SmtpResult(False, "SMTP_CONNECT_FAILED", "password=secret"), MonthlySummaryAttempt.Result.CONFIRMED_FAILURE),
        (SmtpResult(False, "SMTP_RESULT_UNKNOWN", submission_started=True), MonthlySummaryAttempt.Result.UNKNOWN),
    ],
)
def test_send_reuses_transport_and_changes_only_summary(
    ready_company, configuration, settings, tmp_path, monkeypatch, smtp_result, expected
):
    _success_attempt(ready_company, settings, tmp_path)
    attempt = prepare_monthly_summary(ready_company["cycle"], configuration)
    before = {
        "cycle": list(MonthlyCycle.objects.values()),
        "invoices": list(Invoice.objects.values()),
        "deliveries": list(DeliveryAttempt.objects.values()),
        "archives": list(ArchivedFile.objects.values()),
    }

    def fake_transport(prepared, config, ref, *, on_submission_started, **kwargs):
        assert prepared.envelope_sender == prepared.envelope_recipients[0] == configuration.email
        if smtp_result.submission_started:
            on_submission_started()
        return smtp_result

    monkeypatch.setattr("delivery.transport.send_prepared_email", fake_transport)
    smtp_config = resolve_monthly_summary_smtp_config(configuration)
    sent = send_monthly_summary(attempt, smtp_config, object())

    assert sent.result == expected
    assert "secret" not in sent.message
    assert before == {
        "cycle": list(MonthlyCycle.objects.values()),
        "invoices": list(Invoice.objects.values()),
        "deliveries": list(DeliveryAttempt.objects.values()),
        "archives": list(ArchivedFile.objects.values()),
    }


@pytest.mark.parametrize("invalid", ["smtp", "recipient"])
def test_send_rejects_sender_identity_mismatch_before_transport(
    ready_company, configuration, settings, tmp_path, monkeypatch, invalid
):
    _success_attempt(ready_company, settings, tmp_path)
    attempt = prepare_monthly_summary(ready_company["cycle"], configuration)
    if invalid == "recipient":
        MonthlySummaryAttempt.objects.filter(pk=attempt.pk).update(
            recipient="other@example.com"
        )
        smtp_config = resolve_monthly_summary_smtp_config(configuration)
    else:
        smtp_config = SmtpConfig(
            host="smtp.example.com",
            port=587,
            security=SmtpSecurity.STARTTLS,
            timeout_seconds=12.5,
            username="other@example.com",
        )
    called = False

    def forbidden_transport(*args, **kwargs):
        nonlocal called
        called = True
        pytest.fail("transport called")

    monkeypatch.setattr("delivery.transport.send_prepared_email", forbidden_transport)
    with pytest.raises(ValidationError, match="must match"):
        send_monthly_summary(attempt, smtp_config, object())
    attempt.refresh_from_db()
    assert attempt.result == MonthlySummaryAttempt.Result.PREPARED
    assert not called


def test_real_transport_rejects_database_transaction(ready_company, configuration, settings, tmp_path):
    _success_attempt(ready_company, settings, tmp_path)
    attempt = prepare_monthly_summary(ready_company["cycle"], configuration)
    config = resolve_monthly_summary_smtp_config(configuration)
    reference = resolve_monthly_summary_credential_ref(configuration)
    with transaction.atomic():
        result = send_monthly_summary(
            attempt, config, reference,
            password_loader=lambda ref: "secret",
            smtp_factory=lambda config, context: pytest.fail("SMTP factory called"),
        )
    assert result.result == MonthlySummaryAttempt.Result.CONFIRMED_FAILURE
    assert "outside a database transaction" in result.message
