"""Current, durable SMTP connection evidence. No credentials enter this module."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from companies.models import Company
from core.models import AuditLogEntry

from .configuration import (
    SmtpConfigurationError,
    resolve_keyring_credential_ref,
    resolve_monthly_summary_configuration,
    resolve_monthly_summary_credential_ref,
    resolve_monthly_summary_smtp_config,
    resolve_smtp_config,
)
from .models import SmtpVerification
from .transport import SmtpResult


class SmtpVerificationRequired(ValidationError):
    pass


def _message(company_id):
    target = (
        "Nastavitve > Zbirno mesečno poročilo"
        if company_id is None else f"Nastavitve > Podjetje {company_id}"
    )
    return (
        f"SMTP povezava ni potrjena za trenutne nastavitve. V {target} "
        "izvedite Preveri SMTP povezavo. Po menjavi gesla test ponovite."
    )


def current_identity(company_id):
    if company_id is None:
        configuration = resolve_monthly_summary_configuration()
        return (
            resolve_monthly_summary_smtp_config(configuration),
            resolve_monthly_summary_credential_ref(configuration),
        )
    company = Company.objects.get(pk=company_id)
    return resolve_smtp_config(company), resolve_keyring_credential_ref(company)


def configuration_fingerprint(company_id, config, credential_ref):
    payload = {
        "schema": 1,
        "scope": "monthly-summary" if company_id is None else f"company:{company_id}",
        "host": config.host,
        "port": config.port,
        "security": config.security.value,
        "username": config.username,
        "sender": config.username,
        "timeout_seconds": float(config.timeout_seconds),
        "keyring_service": credential_ref.service_name,
        "credential_key": credential_ref.username,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class VerificationStatus:
    state: str
    last_success_at: datetime | None = None

    @property
    def verified(self):
        return self.state == "verified"


def verification_status(company_id):
    records = SmtpVerification.objects.filter(company_id=company_id)
    latest = records.order_by("-pk").first()
    last_success = records.filter(verified_at__isnull=False).order_by("-pk").first()
    last_time = last_success.verified_at if last_success else None
    if getattr(settings, "MARJAN_DESKTOP_TEST", False):
        return VerificationStatus("disabled", last_time)
    if latest is None:
        return VerificationStatus("missing")
    try:
        config, reference = current_identity(company_id)
        valid = (
            not getattr(settings, "MARJAN_DESKTOP_TEST", False)
            and latest.verified_at is not None
            and latest.invalidated_at is None
            and latest.result_code == "SMTP_CONNECTION_OK"
            and latest.sender == config.username
            and latest.fingerprint == configuration_fingerprint(company_id, config, reference)
        )
    except (SmtpConfigurationError, Company.DoesNotExist):
        valid = False
    return VerificationStatus("verified" if valid else "stale", last_time)


def require_verified(company_id, config, credential_ref, sender):
    """Fetch current identity, never trust caller snapshots or another scope."""
    if getattr(settings, "MARJAN_DESKTOP_TEST", False):
        raise SmtpVerificationRequired(
            "SMTP in Keychain sta v namiznem profilu TEST onemogočena. "
            "Potrditve SMTP ni mogoče opraviti."
        )
    try:
        with transaction.atomic():
            current_config, current_ref = current_identity(company_id)
            latest = SmtpVerification.objects.filter(company_id=company_id).order_by("-pk").first()
            fingerprint = configuration_fingerprint(company_id, current_config, current_ref)
            if latest and latest.fingerprint != fingerprint and latest.invalidated_at is None:
                SmtpVerification.objects.filter(pk=latest.pk).update(invalidated_at=timezone.now())
                latest.invalidated_at = timezone.now()
            valid = (
                not getattr(settings, "MARJAN_DESKTOP_TEST", False)
                and config == current_config
                and credential_ref == current_ref
                and sender == current_config.username
                and latest is not None and latest.verified_at is not None
                and latest.invalidated_at is None
                and latest.result_code == "SMTP_CONNECTION_OK"
                and latest.sender == sender
                and latest.fingerprint == fingerprint
            )
    except (SmtpConfigurationError, Company.DoesNotExist, DatabaseError):
        valid = False
    if not valid:
        raise SmtpVerificationRequired(_message(company_id))


def verify_connection(company_id, *, tester=None, **adapters):
    """Revoke before IO; a failed, interrupted or superseded retest fails closed."""
    from .transport import test_smtp_connection

    if getattr(settings, "MARJAN_DESKTOP_TEST", False):
        return SmtpResult(False, "TEST_DELIVERY_DISABLED")
    if connection.in_atomic_block:
        raise ValidationError("SMTP tests must run outside a DB transaction.")
    # Commit revocation separately. If result/audit persistence fails, the pending
    # test remains authoritative and previous approvals cannot silently return.
    with transaction.atomic():
        SmtpVerification.objects.filter(company_id=company_id, invalidated_at=None).update(
            invalidated_at=timezone.now()
        )
    with transaction.atomic():
        config, reference = current_identity(company_id)
        fingerprint = configuration_fingerprint(company_id, config, reference)
        record = SmtpVerification.objects.create(
            company_id=company_id, sender=config.username, fingerprint=fingerprint,
        )
    try:
        result = (tester or test_smtp_connection)(config, reference, **adapters)
    except Exception:  # noqa: BLE001 - never retain provider exceptions or secrets
        result = SmtpResult(False, "SMTP_TEST_FAILED")
    with transaction.atomic():
        latest = SmtpVerification.objects.filter(company_id=company_id).latest("pk")
        try:
            current_config, current_ref = current_identity(company_id)
            unchanged = fingerprint == configuration_fingerprint(company_id, current_config, current_ref)
        except (SmtpConfigurationError, Company.DoesNotExist):
            unchanged = False
        success = (
            result.success and result.code == "SMTP_CONNECTION_OK"
            and latest.pk == record.pk and latest.invalidated_at is None and unchanged
            and not getattr(settings, "MARJAN_DESKTOP_TEST", False)
        )
        # Store only our own fixed result codes, never provider text or arbitrary codes.
        code = "SMTP_CONNECTION_OK" if success else "SMTP_TEST_FAILED"
        SmtpVerification.objects.filter(pk=record.pk).update(
            verified_at=timezone.now() if success else None, result_code=code,
        )
        AuditLogEntry.objects.create(
            content_type=ContentType.objects.get_for_model(SmtpVerification),
            object_id=record.pk, polje="smtp_connection_test", nova_vrednost=code,
            razlog="Explicit non-sending SMTP connection and authentication test; "
            "local operator; scope and sender recorded on the verification record.",
        )
    return SmtpResult(True, code) if success else SmtpResult(
        False, result.code if not result.success else "SMTP_CONFIGURATION_CHANGED",
    )
