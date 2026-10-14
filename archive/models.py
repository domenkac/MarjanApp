from django.core.exceptions import ValidationError
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
    dropbox_root = models.CharField(blank=True, default="")
    verified_at = models.DateTimeField()
    verification_evidence = models.JSONField(default=dict)
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


class SnapshotQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Backup snapshots cannot be overwritten.")

    def delete(self):
        raise ValidationError("Backup snapshots cannot be deleted.")

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValidationError("Backup snapshots cannot be overwritten.")

    def bulk_create(self, objs, **kwargs):
        if kwargs.get("update_conflicts") or kwargs.get("ignore_conflicts"):
            raise ValidationError("Backup snapshots cannot be overwritten.")
        return super().bulk_create(objs, **kwargs)


class BackupSnapshot(models.Model):
    class Trigger(models.TextChoices):
        MANUAL = "MANUAL", "Manual"
        PRE_MONTHLY = "PRE_MONTHLY", "Before monthly process"
        POST_MONTHLY = "POST_MONTHLY", "After monthly process"
        SCHEDULED = "SCHEDULED", "Scheduled"

    path = models.CharField(unique=True)
    zip_checksum = models.CharField(max_length=64)
    database_checksum = models.CharField(max_length=64)
    created_at = models.DateTimeField()
    verified_at = models.DateTimeField()
    trigger = models.CharField(max_length=12, choices=Trigger)
    objects = SnapshotQuerySet.as_manager()

    class Meta:
        base_manager_name = "objects"
        ordering = ["-created_at", "-pk"]  # noqa: RUF012
        constraints = [  # noqa: RUF012
            models.CheckConstraint(
                condition=models.Q(
                    trigger__in=["MANUAL", "PRE_MONTHLY", "POST_MONTHLY", "SCHEDULED"]
                ),
                name="backup_snapshot_valid_trigger",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding or (
            self.pk and type(self).objects.filter(pk=self.pk).exists()
        ):
            raise ValidationError("Backup snapshots cannot be overwritten.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Backup snapshots cannot be deleted.")
