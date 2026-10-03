from django.core.exceptions import ValidationError
from django.db import models


class DeliveryAttempt(models.Model):
    class Result(models.TextChoices):
        PREPARED = "PREPARED", "Prepared"
        SUBMISSION_STARTED = "SUBMISSION_STARTED", "Submission started"
        ACCEPTED = "ACCEPTED", "Accepted"
        CONFIRMED_FAILURE = "CONFIRMED_FAILURE", "Confirmed failure"
        UNKNOWN = "UNKNOWN", "Unknown"

    invoice = models.ForeignKey(
        "invoicing.Invoice",
        on_delete=models.PROTECT,
        related_name="delivery_attempts",
    )
    sequence = models.PositiveIntegerField()
    prepared_at = models.DateTimeField(auto_now_add=True)
    submission_started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    sender = models.EmailField()
    recipients_to = models.JSONField(default=list, blank=True)
    recipients_cc = models.JSONField(default=list, blank=True)
    recipients_bcc = models.JSONField(default=list, blank=True)
    pdf_path = models.CharField()
    pdf_checksum = models.CharField(max_length=64)
    result = models.CharField(
        max_length=24,
        choices=Result,
        default=Result.PREPARED,
    )
    result_code = models.CharField(max_length=64, blank=True)
    message = models.TextField(blank=True)

    class Meta:
        ordering = ["invoice_id", "sequence"]  # noqa: RUF012
        constraints = [  # noqa: RUF012
            models.UniqueConstraint(
                fields=["invoice", "sequence"],
                name="uniq_delivery_attempt_invoice_sequence",
            ),
            models.CheckConstraint(
                condition=models.Q(sequence__gte=1),
                name="delivery_attempt_sequence_gte_1",
            ),
        ]
        indexes = [  # noqa: RUF012
            models.Index(fields=["invoice", "result"], name="delivery_invoice_result_idx")
        ]

    def clean(self):
        super().clean()
        errors = {}
        if not self.recipients_to:
            errors["recipients_to"] = "At least one To recipient is required."
        for field in ("recipients_to", "recipients_cc", "recipients_bcc"):
            value = getattr(self, field)
            if not isinstance(value, list) or any(
                not isinstance(address, str) or not address.strip()
                for address in value
            ):
                errors[field] = "Recipients must be a list of non-empty email strings."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Delivery attempts are audit records and cannot be overwritten.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Delivery attempts are audit records and cannot be deleted.")


class MonthlySummaryConfiguration(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    email = models.EmailField()
    keychain_credential_key = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [  # noqa: RUF012
            models.CheckConstraint(
                condition=models.Q(id=1),
                name="monthly_summary_configuration_singleton",
            )
        ]

    def save(self, *args, **kwargs):
        if self.pk not in (None, 1):
            raise ValidationError("Monthly summary configuration must use pk=1.")
        self.pk = 1
        self.full_clean()
        return super().save(*args, **kwargs)


class MonthlySummaryAttempt(models.Model):
    class Result(models.TextChoices):
        PREPARED = "PREPARED", "Prepared"
        SUBMISSION_STARTED = "SUBMISSION_STARTED", "Submission started"
        ACCEPTED = "ACCEPTED", "Accepted"
        CONFIRMED_FAILURE = "CONFIRMED_FAILURE", "Confirmed failure"
        UNKNOWN = "UNKNOWN", "Unknown"

    cycle = models.ForeignKey(
        "invoicing.MonthlyCycle",
        on_delete=models.PROTECT,
        related_name="summary_attempts",
    )
    sequence = models.PositiveIntegerField()
    prepared_at = models.DateTimeField(auto_now_add=True)
    submission_started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    sender = models.EmailField()
    recipient = models.EmailField()
    subject = models.CharField(max_length=255)
    body = models.TextField()
    summary_snapshot = models.JSONField()
    result = models.CharField(max_length=24, choices=Result, default=Result.PREPARED)
    result_code = models.CharField(max_length=64, blank=True)
    message = models.TextField(blank=True)

    class Meta:
        ordering = ["cycle_id", "sequence"]  # noqa: RUF012
        constraints = [  # noqa: RUF012
            models.UniqueConstraint(
                fields=["cycle", "sequence"],
                name="uniq_monthly_summary_cycle_sequence",
            ),
            models.CheckConstraint(
                condition=models.Q(sequence__gte=1),
                name="monthly_summary_sequence_gte_1",
            ),
            models.UniqueConstraint(
                fields=["cycle"],
                condition=models.Q(result="ACCEPTED"),
                name="uniq_accepted_monthly_summary_cycle",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Monthly summary attempts are audit records and cannot be overwritten.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Monthly summary attempts are audit records and cannot be deleted.")
