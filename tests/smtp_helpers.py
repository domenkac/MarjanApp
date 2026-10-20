"""Explicit synthetic connection evidence for established workflow fixtures."""

from django.conf import settings
from django.utils import timezone

from delivery.models import SmtpVerification
from delivery.verification import configuration_fingerprint, current_identity


def approve_smtp(company_id, *, config=None):
    if config is not None:
        settings.MARJAN_SMTP_HOST = config.host
        settings.MARJAN_SMTP_PORT = config.port
        settings.MARJAN_SMTP_SECURITY = config.security.value
        settings.MARJAN_SMTP_TIMEOUT_SECONDS = config.timeout_seconds
    current, reference = current_identity(company_id)
    return SmtpVerification.objects.create(
        company_id=company_id, sender=current.username,
        fingerprint=configuration_fingerprint(company_id, current, reference),
        verified_at=timezone.now(), result_code="SMTP_CONNECTION_OK",
    )
