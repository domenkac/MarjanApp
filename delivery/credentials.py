from dataclasses import dataclass

import keyring
from django.conf import settings
from django.db import connection


@dataclass(frozen=True, slots=True)
class KeyringCredentialRef:
    service_name: str
    username: str

    def __post_init__(self):
        for name in ("service_name", "username"):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or not value.strip()
                or value != value.strip()
            ):
                raise ValueError(f"{name} must be a non-empty trimmed string.")


class CredentialError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def load_password(ref: KeyringCredentialRef) -> str:
    if getattr(settings, "MARJAN_DESKTOP_TEST", False):
        raise CredentialError("TEST_DELIVERY_DISABLED", "Keychain is disabled in desktop TEST.")
    if connection.in_atomic_block:
        raise RuntimeError("Keyring lookup must run outside a database transaction.")
    try:
        password = keyring.get_password(ref.service_name, ref.username)
    except Exception:  # noqa: BLE001 -- backend failures must expose no details
        raise CredentialError(
            "KEYCHAIN_ERROR", "Keychain credential lookup failed."
        ) from None
    if password is None:
        raise CredentialError(
            "KEYCHAIN_CREDENTIAL_MISSING", "Keychain credential is missing."
        )
    if not isinstance(password, str) or not password:
        raise CredentialError(
            "KEYCHAIN_ERROR", "Keychain returned an invalid credential."
        )
    return password
