from django.db import models


class ArchivedFile(models.Model):
    class ArtifactType(models.TextChoices):
        PDF = "PDF", "PDF"
        ESLOG = "ESLOG", "eSLOG"

    class Status(models.TextChoices):
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        VERIFIED = "VERIFIED", "Verified"

    invoice = models.ForeignKey(
        "invoicing.Invoice", on_delete=models.PROTECT, related_name="archived_files"
    )
    artifact_type = models.CharField(max_length=8, choices=ArtifactType)
    local_path = models.CharField()
    icloud_path = models.CharField()
    checksum = models.CharField(max_length=64)
    status = models.CharField(max_length=11, choices=Status, default=Status.IN_PROGRESS)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [  # noqa: RUF012
            models.UniqueConstraint(
                fields=["invoice", "artifact_type"],
                name="uniq_archived_file_invoice_artifact",
            )
        ]


class ArchiveConfiguration(models.Model):
    local_root = models.CharField()
    icloud_root = models.CharField()
    verified_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [  # noqa: RUF012
            models.CheckConstraint(
                condition=models.Q(id=1),
                name="archive_configuration_singleton",
            )
        ]

    def save(self, *args, **kwargs):
        self.pk = 1
        return super().save(*args, **kwargs)
