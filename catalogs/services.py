from django.core.exceptions import ValidationError
from django.db import models, transaction

from core.services import (
    StanjeRazlog,
    StanjeRezultat,
    audit_field_changes,
    financial_changes,
    save_business_edit,
    validate_edit_source,
)

from .models import ServiceCatalogItem, VatRegime

VAT_FINANCIAL_FIELDS = tuple(
    field.name for field in VatRegime._meta.concrete_fields if field.name != "id"
)


def save_service_catalog_item_edit(item, *, source_fingerprint, razlog=""):
    return save_business_edit(
        item, ServiceCatalogItem.business_edit_fields, source_fingerprint=source_fingerprint, razlog=razlog,
    )


@transaction.atomic
def save_vat_regime_edit(regime, *, source_fingerprint, razlog=""):
    previous = VatRegime.objects.select_for_update().get(pk=regime.pk)
    validate_edit_source(previous, source_fingerprint)
    regime.full_clean()
    changes = financial_changes(previous, regime, VAT_FINANCIAL_FIELDS, razlog)
    models.Model.save(regime)
    regime.refresh_from_db()
    audit_field_changes(regime, changes, razlog)
    return regime


def _spremeni_stanje(model, pk, field, value):
    with transaction.atomic():
        zapis = model.objects.select_for_update().get(pk=pk)
        previous = getattr(zapis, field)
        setattr(zapis, field, value)
        try:
            zapis.full_clean()
        except ValidationError as error:
            return StanjeRezultat(
                False,
                tuple(
                    StanjeRazlog("NEVELJAVEN_ZAPIS", message)
                    for messages in getattr(
                        error, "message_dict", {"__all__": error.messages}
                    ).values()
                    for message in messages
                ),
            )
        models.Model.save(zapis, update_fields=[field])
        label = "DDV režima" if model is VatRegime else "storitve"
        audit_field_changes(
            zapis, {field: previous},
            f"Ponovna aktivacija {label}" if value else f"Deaktiviranje {label}",
        )
    return StanjeRezultat(True)


def deaktiviraj_ddv_rezim(pk):
    return _spremeni_stanje(VatRegime, pk, "aktiven", False)


def ponovno_aktiviraj_ddv_rezim(pk):
    return _spremeni_stanje(VatRegime, pk, "aktiven", True)


def deaktiviraj_storitev(pk):
    return _spremeni_stanje(ServiceCatalogItem, pk, "aktivna", False)


def ponovno_aktiviraj_storitev(pk):
    return _spremeni_stanje(ServiceCatalogItem, pk, "aktivna", True)
