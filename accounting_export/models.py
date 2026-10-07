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


class _ImmutablePdfArtifactQuerySet(models.QuerySet):
    def bulk_create(self, objs, **kwargs):
        if kwargs.get("update_conflicts"):
            raise ValidationError("Accountant PDF artifact metadata is immutable.")
        return super().bulk_create(objs, **kwargs)

    def update(self, **kwargs):
        raise ValidationError("Accountant PDF artifact metadata is immutable.")

    def delete(self):
        raise ValidationError("Accountant PDF artifacts cannot be deleted.")


class AccountantReportPdfArtifact(models.Model):
    report = models.OneToOneField(
        AccountantReport, on_delete=models.PROTECT, related_name="pdf_artifact"
    )
    local_path = models.CharField()
    checksum = models.CharField(max_length=64)
    verified_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    objects = _ImmutablePdfArtifactQuerySet.as_manager()

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Accountant PDF artifact metadata is immutable.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Accountant PDF artifacts cannot be deleted.")
