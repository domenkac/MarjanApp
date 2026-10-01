from django.conf import settings

from .credentials import KeyringCredentialRef
from .transport import SmtpConfig, SmtpSecurity


class SmtpConfigurationError(Exception):
    pass


def _setting(name):
    value = getattr(settings, name, None)
    if value is None or isinstance(value, str) and (not value or value != value.strip()):
        raise SmtpConfigurationError("SMTP konfiguracija ni nastavljena.")
    return value


def resolve_smtp_config(company) -> SmtpConfig:
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
            username=company.email_posiljatelj,
        )
    except (TypeError, ValueError) as exc:
        raise SmtpConfigurationError("SMTP konfiguracija ni nastavljena.") from exc


def resolve_keyring_credential_ref(company) -> KeyringCredentialRef:
    try:
        return KeyringCredentialRef(
            _setting("MARJAN_KEYRING_SERVICE"),
            company.keychain_kljuc_posiljatelja,
        )
    except (TypeError, ValueError) as exc:
        raise SmtpConfigurationError("SMTP konfiguracija ni nastavljena.") from exc
