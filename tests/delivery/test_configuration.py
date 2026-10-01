import pytest

from companies.models import Company
from delivery.configuration import (
    SmtpConfigurationError,
    resolve_keyring_credential_ref,
    resolve_smtp_config,
)
from delivery.credentials import KeyringCredentialRef
from delivery.transport import SmtpConfig, SmtpSecurity


@pytest.fixture
def company():
    return Company(
        email_posiljatelj="sender@example.com",
        keychain_kljuc_posiljatelja="smtp-company-key",
    )


@pytest.fixture
def smtp_settings(settings):
    settings.MARJAN_SMTP_HOST = "smtp.example.com"
    settings.MARJAN_SMTP_PORT = "587"
    settings.MARJAN_SMTP_SECURITY = "STARTTLS"
    settings.MARJAN_SMTP_TIMEOUT_SECONDS = "12.5"
    settings.MARJAN_KEYRING_SERVICE = "marjan-smtp"


def test_resolves_smtp_config(company, smtp_settings):
    assert resolve_smtp_config(company) == SmtpConfig(
        host="smtp.example.com",
        port=587,
        security=SmtpSecurity.STARTTLS,
        timeout_seconds=12.5,
        username="sender@example.com",
    )


def test_resolves_keyring_reference(company, smtp_settings):
    assert resolve_keyring_credential_ref(company) == KeyringCredentialRef(
        "marjan-smtp", "smtp-company-key"
    )


@pytest.mark.parametrize(
    ("name", "value", "resolver"),
    [
        ("MARJAN_SMTP_HOST", "", resolve_smtp_config),
        ("MARJAN_SMTP_HOST", " smtp.example.com", resolve_smtp_config),
        ("MARJAN_SMTP_PORT", "0", resolve_smtp_config),
        ("MARJAN_SMTP_PORT", "invalid", resolve_smtp_config),
        ("MARJAN_SMTP_SECURITY", "TLS", resolve_smtp_config),
        ("MARJAN_SMTP_SECURITY", "starttls", resolve_smtp_config),
        ("MARJAN_SMTP_TIMEOUT_SECONDS", "0", resolve_smtp_config),
        ("MARJAN_SMTP_TIMEOUT_SECONDS", "invalid", resolve_smtp_config),
        ("MARJAN_KEYRING_SERVICE", "", resolve_keyring_credential_ref),
        ("MARJAN_KEYRING_SERVICE", " marjan", resolve_keyring_credential_ref),
    ],
)
def test_invalid_runtime_settings_fail_closed(
    company, smtp_settings, settings, name, value, resolver
):
    setattr(settings, name, value)

    with pytest.raises(SmtpConfigurationError, match="SMTP konfiguracija ni nastavljena"):
        resolver(company)
