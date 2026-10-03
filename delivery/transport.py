from __future__ import annotations

import math
import smtplib
import socket
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from django.db import connection

from .credentials import CredentialError, KeyringCredentialRef, load_password


class PreparedOutboundEmail(Protocol):
    message: object
    envelope_sender: str
    envelope_recipients: tuple[str, ...]


class SmtpSecurity(StrEnum):
    NONE = "NONE"
    STARTTLS = "STARTTLS"
    SSL = "SSL"


@dataclass(frozen=True, slots=True)
class SmtpConfig:
    host: str
    port: int
    security: SmtpSecurity
    timeout_seconds: float
    username: str

    def __post_init__(self):
        for name in ("host", "username"):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or not value.strip()
                or value != value.strip()
            ):
                raise ValueError(f"{name} must be a non-empty trimmed string.")
        if (
            isinstance(self.port, bool)
            or not isinstance(self.port, int)
            or not 1 <= self.port <= 65535
        ):
            raise ValueError("port must be an integer from 1 through 65535.")
        if not isinstance(self.security, SmtpSecurity):
            raise TypeError("security must be a SmtpSecurity value.")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a positive finite number.")


@dataclass(frozen=True, slots=True)
class SmtpResult:
    success: bool
    code: str
    message: str = ""
    submission_started: bool = False


SmtpFactory = Callable[[SmtpConfig, ssl.SSLContext], smtplib.SMTP]


def _default_smtp_factory(config: SmtpConfig, context: ssl.SSLContext):
    if config.security is SmtpSecurity.SSL:
        return smtplib.SMTP_SSL(
            config.host,
            config.port,
            timeout=config.timeout_seconds,
            context=context,
        )
    return smtplib.SMTP(config.host, config.port, timeout=config.timeout_seconds)


def _outside_transaction():
    if connection.in_atomic_block:
        raise RuntimeError("SMTP operations must run outside a database transaction.")


def _text(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _safe(value, password: str) -> str:
    return _text(value).replace(password, "[REDACTED]") if password else _text(value)


def _open(config, password, smtp_factory):
    _outside_transaction()
    context = ssl.create_default_context()
    smtp = smtp_factory(config, context)
    try:
        if config.security is not SmtpSecurity.SSL:
            smtp.ehlo()
        if config.security is SmtpSecurity.STARTTLS:
            smtp.starttls(context=context)
            smtp.ehlo()
        smtp.login(config.username, password)
        return smtp
    except Exception:
        _cleanup(smtp)
        raise


def _cleanup(smtp, *, reset=False):
    if smtp is None:
        return
    if reset:
        try:
            smtp.rset()
        except (OSError, smtplib.SMTPException):
            pass  # Best-effort cleanup must not replace delivery result.
    try:
        smtp.quit()
    except (OSError, smtplib.SMTPException):
        try:
            smtp.close()
        except (OSError, smtplib.SMTPException):
            pass  # Best-effort cleanup must not replace delivery result.


def _pre_data_error(exc, password: str) -> SmtpResult:
    if isinstance(exc, CredentialError):
        return SmtpResult(False, exc.code, str(exc))
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return SmtpResult(False, "SMTP_AUTH_FAILED", _safe(exc.smtp_error, password))
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return SmtpResult(False, "SMTP_TIMEOUT", "SMTP operation timed out.")
    if isinstance(
        exc,
        (ConnectionRefusedError, smtplib.SMTPConnectError, socket.gaierror),
    ):
        return SmtpResult(False, "SMTP_CONNECT_FAILED", "SMTP server is unreachable.")
    if isinstance(
        exc,
        (
            smtplib.SMTPServerDisconnected,
            ConnectionResetError,
            BrokenPipeError,
            ConnectionAbortedError,
        ),
    ):
        return SmtpResult(False, "SMTP_CONNECTION_LOST", "SMTP connection was lost.")
    if isinstance(exc, OSError):
        return SmtpResult(False, "SMTP_CONNECT_FAILED", "SMTP server is unreachable.")
    return SmtpResult(False, "SMTP_PROTOCOL_ERROR", _safe(exc, password))


def test_smtp_connection(
    config: SmtpConfig,
    credential_ref: KeyringCredentialRef,
    *,
    password_loader=load_password,
    smtp_factory: SmtpFactory = _default_smtp_factory,
) -> SmtpResult:
    smtp = None
    password = ""
    try:
        password = password_loader(credential_ref)
        smtp = _open(config, password, smtp_factory)
        return SmtpResult(True, "SMTP_CONNECTION_OK")
    except Exception as exc:  # noqa: BLE001 - adapter returns structured failures
        return _pre_data_error(exc, password)
    finally:
        _cleanup(smtp)


def send_prepared_email(
    prepared: PreparedOutboundEmail,
    config: SmtpConfig,
    credential_ref: KeyringCredentialRef,
    *,
    on_submission_started: Callable[[], None],
    password_loader=load_password,
    smtp_factory: SmtpFactory = _default_smtp_factory,
) -> SmtpResult:
    payload = prepared.message.as_bytes(
        policy=prepared.message.policy.clone(linesep="\r\n")
    )
    smtp = None
    password = ""
    submission_started = False
    final_result = None
    try:
        password = password_loader(credential_ref)
        smtp = _open(config, password, smtp_factory)

        code, reply = smtp.mail(prepared.envelope_sender)
        if not 200 <= code < 300:
            return SmtpResult(False, "SMTP_SENDER_REJECTED", _safe(reply, password))

        for recipient in prepared.envelope_recipients:
            code, reply = smtp.rcpt(recipient)
            if not 200 <= code < 300:
                return SmtpResult(
                    False,
                    "SMTP_RECIPIENT_REJECTED",
                    f"{recipient}: {code} {_safe(reply, password)}",
                )

        on_submission_started()
        submission_started = True
        code, reply = smtp.data(payload)
        if 200 <= code < 300:
            final_result = SmtpResult(
                True, "SMTP_ACCEPTED", _safe(reply, password), True
            )
        else:
            final_result = SmtpResult(
                False, "SMTP_DATA_REJECTED", f"{code} {_safe(reply, password)}", True
            )
        return final_result
    except smtplib.SMTPDataError as exc:
        return SmtpResult(
            False,
            "SMTP_DATA_REJECTED",
            f"{exc.smtp_code} {_safe(exc.smtp_error, password)}",
            True,
        )
    except Exception as exc:  # noqa: BLE001 - unknown after DATA is business state
        if submission_started:
            return SmtpResult(
                False,
                "SMTP_RESULT_UNKNOWN",
                "SMTP connection ended before a definitive DATA response.",
                True,
            )
        return _pre_data_error(exc, password)
    finally:
        _cleanup(smtp, reset=final_result is None and not submission_started)
