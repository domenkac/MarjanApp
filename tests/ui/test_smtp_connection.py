import pytest
from django.db import DatabaseError, connection
from django.test import Client
from django.urls import reverse

from companies.models import Company
from core.models import AuditLogEntry
from delivery.credentials import KeyringCredentialRef
from delivery.models import (
    DeliveryAttempt,
    MonthlySummaryConfiguration,
    SmtpVerification,
)
from delivery.transport import SmtpConfig, SmtpResult, SmtpSecurity
from delivery.verification import verification_status
from invoicing.models import Invoice, MonthlyCycle

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def company(db):
    return Company.objects.create(
        naziv="Alfa d.o.o.",
        davcna_stevilka="15012557",
        maticna_stevilka="1234567000",
        naslov="Glavna ulica 1",
        drzava_koda="SI",
        trr="SI56191000000123438",
        logotip="logo.png",
        ddv_zavezanec=True,
        email_racunovodja="accountant@example.com",
        email_posiljatelj="sender@example.com",
        keychain_kljuc_posiljatelja="smtp-company-key",
        rok_placila_dni_privzeto=15,
    )


@pytest.fixture(autouse=True)
def smtp_settings(settings):
    settings.MARJAN_SMTP_HOST = "smtp.example.com"
    settings.MARJAN_SMTP_PORT = 587
    settings.MARJAN_SMTP_SECURITY = "STARTTLS"
    settings.MARJAN_SMTP_TIMEOUT_SECONDS = 10
    settings.MARJAN_KEYRING_SERVICE = "marjan-smtp"


def test_get_displays_non_secret_config_without_testing(client, company, monkeypatch):
    tested = False

    def forbidden(*args, **kwargs):
        nonlocal tested
        tested = True

    monkeypatch.setattr("ui.views.check_smtp_connection", forbidden)
    content = client.get(reverse("ui:podjetje_detail", args=[company.pk])).content.decode()

    assert not tested
    assert "smtp.example.com" in content
    assert "587" in content
    assert "STARTTLS" in content
    assert "sender@example.com" in content
    assert "Preveri SMTP povezavo" in content
    assert "smtp-company-key" in content  # Ordinary Company form field only.
    assert "top-secret" not in content


@pytest.mark.django_db(transaction=True)
def test_post_tests_connection_outside_transaction_without_business_mutation(
    client, settings, monkeypatch
):
    company = Company.objects.create(
        naziv="Alfa d.o.o.", davcna_stevilka="15012557", maticna_stevilka="1234567000",
        naslov="Glavna ulica 1", drzava_koda="SI", trr="SI56191000000123438",
        logotip="logo.png", ddv_zavezanec=True, email_racunovodja="a@example.com",
        email_posiljatelj="sender@example.com", keychain_kljuc_posiljatelja="smtp-company-key",
        rok_placila_dni_privzeto=15,
    )
    expected_config = SmtpConfig(
        "smtp.example.com", 587, SmtpSecurity.STARTTLS, 10.0, "sender@example.com"
    )
    expected_ref = KeyringCredentialRef("marjan-smtp", "smtp-company-key")
    before = (
        list(DeliveryAttempt.objects.values()),
        list(Invoice.objects.values()),
        list(MonthlyCycle.objects.values()),
    )

    def fake_test(config, credential_ref):
        assert not connection.in_atomic_block
        assert config == expected_config
        assert credential_ref == expected_ref
        return SmtpResult(True, "SMTP_CONNECTION_OK")

    monkeypatch.setattr("ui.views.check_smtp_connection", fake_test)
    monkeypatch.setattr(
        "delivery.transport.send_prepared_email",
        lambda *args, **kwargs: pytest.fail("email send called"),
    )

    response = client.post(reverse("ui:test_smtp_connection", args=[company.pk]))

    assert response.status_code == 200
    assert "SMTP povezava in prijava sta uspešni." in response.content.decode()
    assert before == (
        list(DeliveryAttempt.objects.values()),
        list(Invoice.objects.values()),
        list(MonthlyCycle.objects.values()),
    )


@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("KEYCHAIN_CREDENTIAL_MISSING", "Geslo SMTP ni shranjeno v Keychainu."),
        ("SMTP_AUTH_FAILED", "Prijava v SMTP ni uspela."),
        ("SMTP_CONNECT_FAILED", "SMTP strežnik ni dosegljiv."),
        ("SMTP_TIMEOUT", "Povezava s SMTP strežnikom je potekla."),
        ("SMTP_CONNECTION_LOST", "Povezava s SMTP strežnikom je bila prekinjena."),
    ],
)
def test_structured_failures_are_useful_and_sanitized(
    client, company, monkeypatch, code, message
):
    monkeypatch.setattr(
        "ui.views.check_smtp_connection",
        lambda config, credential_ref: SmtpResult(False, code, "top-secret"),
    )

    response = client.post(reverse("ui:test_smtp_connection", args=[company.pk]))
    content = response.content.decode()

    assert response.status_code == 502
    assert message in content
    assert "top-secret" not in content


def test_invalid_config_does_not_open_connection(client, company, settings, monkeypatch):
    settings.MARJAN_SMTP_HOST = ""
    monkeypatch.setattr(
        "ui.views.check_smtp_connection",
        lambda *args, **kwargs: pytest.fail("network connection attempted"),
    )

    response = client.post(reverse("ui:test_smtp_connection", args=[company.pk]))

    assert response.status_code == 400
    assert "SMTP konfiguracija ni nastavljena." in response.content.decode()


def test_connection_test_is_post_only(client, company, monkeypatch):
    monkeypatch.setattr(
        "ui.views.check_smtp_connection",
        lambda *args, **kwargs: pytest.fail("connection test called"),
    )
    assert client.get(reverse("ui:test_smtp_connection", args=[company.pk])).status_code == 405


@pytest.mark.parametrize("monthly", [False, True])
def test_persistent_status_and_csrf(client, company, monkeypatch, monthly):
    if monthly:
        MonthlySummaryConfiguration.objects.create(email="owner@example.com", keychain_credential_key="owner")
    scope = None if monthly else company.pk
    action = reverse("ui:test_monthly_summary_smtp_connection") if monthly else reverse("ui:test_smtp_connection", args=[company.pk])
    page = reverse("ui:nastavitve_zbirno_porocilo") if monthly else reverse("ui:podjetje_detail", args=[company.pk])
    calls = []
    monkeypatch.setattr("ui.views.check_smtp_connection", lambda *_: calls.append("test") or SmtpResult(True, "SMTP_CONNECTION_OK"))
    assert "Potrditev SMTP povezave manjka" in client.get(page).content.decode()
    assert not calls and not SmtpVerification.objects.exists()
    assert Client(enforce_csrf_checks=True).post(action).status_code == 403
    assert not calls and not SmtpVerification.objects.exists()
    assert client.post(action).status_code == 200
    assert verification_status(scope).verified
    assert "Zadnja uspešna potrditev" in client.get(page).content.decode()
    assert calls == ["test"]
    assert client.get(action).status_code == 405
    assert calls == ["test"] and SmtpVerification.objects.count() == 1


def test_wrong_issuer_does_not_test_or_record(client, company, monkeypatch):
    monkeypatch.setattr("ui.views.check_smtp_connection", lambda *_: pytest.fail("SMTP"))
    assert client.post(reverse("ui:test_smtp_connection", args=[company.pk + 1000])).status_code == 404
    assert not SmtpVerification.objects.exists()


def test_evidence_write_failure_is_sanitized_and_unapproved(client, company, monkeypatch):
    monkeypatch.setattr("ui.views.check_smtp_connection", lambda *_: SmtpResult(True, "SMTP_CONNECTION_OK"))
    def fail(*_, **__):
        raise DatabaseError("private provider details")
    monkeypatch.setattr(AuditLogEntry.objects, "create", fail)
    response = client.post(reverse("ui:test_smtp_connection", args=[company.pk]))
    assert response.status_code == 502
    assert "Potrditve SMTP ni mogoče shraniti" in response.content.decode()
    assert "private provider details" not in response.content.decode()
    assert not verification_status(company.pk).verified
