from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import models


class AuditLogEntry(models.Model):
    class Dejanje(models.TextChoices):
        SPREMEMBA = "SPREMEMBA", "Sprememba"
        FIZICNI_IZBRIS = "FIZICNI_IZBRIS", "Fizični izbris"

    content_type = models.ForeignKey(ContentType, on_delete=models.PROTECT)
    object_id = models.PositiveIntegerField()
    dejanje = models.CharField(
        max_length=14, choices=Dejanje, default=Dejanje.SPREMEMBA
    )
    polje = models.CharField(blank=True)
    stara_vrednost = models.CharField(blank=True)
    nova_vrednost = models.CharField(blank=True)
    razlog = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [  # noqa: RUF012
            models.Index(
                fields=["content_type", "object_id", "polje", "created_at"],
                name="audit_change_lookup_idx",
            ),
            models.Index(
                fields=["content_type", "object_id", "dejanje", "created_at"],
                name="audit_action_lookup_idx",
            ),
        ]

    def clean(self):
        super().clean()
        napake = {}
        if not (self.razlog or "").strip():
            napake["razlog"] = "Razlog je obvezen."
        if self.dejanje == self.Dejanje.FIZICNI_IZBRIS:
            for polje in ("polje", "stara_vrednost", "nova_vrednost"):
                if getattr(self, polje):
                    napake[polje] = "Pri fizičnem izbrisu mora biti polje prazno."
        if napake:
            raise ValidationError(napake)
