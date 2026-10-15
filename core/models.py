from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import models


def financial_field_names(model):
    fields = getattr(model, "financial_fields", None)
    return {
        name
        for field in model._meta.concrete_fields
        if not field.primary_key and (fields is None or field.name in fields)
        for name in (field.name, field.attname)
    }


def validate_financial_save(record, update_fields=None, using=None):
    if record.pk is None or update_fields is not None and not update_fields:
        return
    previous = type(record)._base_manager.using(using or record._state.db).filter(pk=record.pk).first()
    if previous is None:
        return
    protected = financial_field_names(type(record))
    for field in record._meta.concrete_fields:
        if field.name not in protected or (
            update_fields is not None and field.name not in update_fields and field.attname not in update_fields
        ):
            continue
        if getattr(previous, field.attname) != field.to_python(getattr(record, field.attname)):
            raise ValidationError("Finančne spremembe zahtevajo razlog in namensko revizijsko storitev.")


class FinancialQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if financial_field_names(self.model).intersection(kwargs):
            raise ValidationError("Finančne spremembe zahtevajo namensko revizijsko storitev.")
        return super().update(**kwargs)

    def bulk_update(self, objs, fields, batch_size=None):
        if financial_field_names(self.model).intersection(fields):
            raise ValidationError("Finančne spremembe zahtevajo namensko revizijsko storitev.")
        return super().bulk_update(objs, fields, batch_size=batch_size)

    def bulk_create(self, objs, **kwargs):
        if kwargs.get("update_conflicts") and financial_field_names(self.model).intersection(kwargs.get("update_fields") or ()):
            raise ValidationError("Finančne spremembe zahtevajo namensko revizijsko storitev.")
        return super().bulk_create(objs, **kwargs)


class AuditLogQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Revizijska zgodovina je nespremenljiva.")

    def delete(self):
        raise ValidationError("Revizijske zgodovine ni dovoljeno brisati.")

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValidationError("Revizijska zgodovina je nespremenljiva.")

    def bulk_create(self, objs, **kwargs):
        if kwargs.get("update_conflicts"):
            raise ValidationError("Revizijska zgodovina je nespremenljiva.")
        for entry in objs:
            entry.full_clean()
        return super().bulk_create(objs, **kwargs)


class AuditLogEntry(models.Model):
    objects = AuditLogQuerySet.as_manager()

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

    def save(self, *args, **kwargs):
        if self.pk is not None and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError("Revizijska zgodovina je nespremenljiva.")
        self.full_clean()
        kwargs["force_insert"] = True
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Revizijske zgodovine ni dovoljeno brisati.")

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
