from django.conf import settings
from django.core.exceptions import ValidationError

from .credentials import KeyringCredentialRef
from .models import MonthlySummaryConfiguration
from .transport import SmtpConfig, SmtpSecurity


class SmtpConfigurationError(Exception):
    pass


def _setting(name):
    value = getattr(settings, name, None)
    if value is None or isinstance(value, str) and (not value or value != value.strip()):
        raise SmtpConfigurationError("SMTP konfiguracija ni nastavljena.")
    return value


def resolve_smtp_config_for_username(username) -> SmtpConfig:
    try:
        raw_port = _setting("MARJAN_SMTP_PORT")
        if isinstance(raw_port, bool) or not isinstance(raw_port, (int, str)):
            raise TypeError
        if (
            isinstance(raw_port, str) and not raw_port.isascii()
        ) or not str(raw_port).isdigit():
            raise ValueError
        port = int(raw_port)
        raw_timeout = _setting("MARJAN_SMTP_TIMEOUT_SECONDS")
        if isinstance(raw_timeout, bool):
            raise TypeError
        timeout = float(raw_timeout)
        security = SmtpSecurity(_setting("MARJAN_SMTP_SECURITY"))
        return SmtpConfig(
            host=_setting("MARJAN_SMTP_HOST"),
            port=port,
            security=security,
            timeout_seconds=timeout,
            username=username,
        )
    except (TypeError, ValueError) as exc:
        raise SmtpConfigurationError("SMTP konfiguracija ni nastavljena.") from exc


def resolve_smtp_config(company) -> SmtpConfig:
    return resolve_smtp_config_for_username(company.email_posiljatelj)


def resolve_keyring_credential_ref_for_key(credential_key) -> KeyringCredentialRef:
    try:
        return KeyringCredentialRef(
            _setting("MARJAN_KEYRING_SERVICE"),
            credential_key,
        )
    except (TypeError, ValueError) as exc:
        raise SmtpConfigurationError("SMTP konfiguracija ni nastavljena.") from exc


def resolve_keyring_credential_ref(company) -> KeyringCredentialRef:
    return resolve_keyring_credential_ref_for_key(company.keychain_kljuc_posiljatelja)


def resolve_monthly_summary_configuration(
    configuration=None,
) -> MonthlySummaryConfiguration:
    try:
        if configuration is not None and (
            not isinstance(configuration, MonthlySummaryConfiguration)
            or configuration._state.adding
            or configuration.pk != 1
        ):
            raise ValidationError("Monthly summary configuration is not persisted.")
        persisted = MonthlySummaryConfiguration.objects.get(pk=1)
        persisted.full_clean()
        if configuration is not None and (
            configuration.email != persisted.email
            or configuration.keychain_credential_key
            != persisted.keychain_credential_key
        ):
            raise ValidationError("Monthly summary configuration has unsaved changes.")
        return persisted
    except (MonthlySummaryConfiguration.DoesNotExist, ValidationError) as exc:
        raise SmtpConfigurationError("SMTP konfiguracija ni nastavljena.") from exc


def resolve_monthly_summary_smtp_config(configuration) -> SmtpConfig:
    return resolve_smtp_config_for_username(configuration.email)


def resolve_monthly_summary_credential_ref(configuration) -> KeyringCredentialRef:
    return resolve_keyring_credential_ref_for_key(configuration.keychain_credential_key)
