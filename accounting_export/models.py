from django.core.exceptions import ValidationError
from django.db import models


class AccountantReport(models.Model):
    company = models.ForeignKey("companies.Company", on_delete=models.PROTECT)
    period = models.DateField()
    generated_at = models.DateTimeField()
    schema_version = models.PositiveSmallIntegerField()
    snapshot = models.JSONField()

    class Meta:
        ordering = ["company_id", "period", "generated_at", "pk"]  # noqa: RUF012

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Accountant reports are immutable and cannot be overwritten.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Accountant reports are immutable and cannot be deleted.")
