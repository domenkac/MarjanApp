from django.core.exceptions import ValidationError
from django.db import transaction

from core.services import StanjeRazlog, StanjeRezultat

from .models import ServiceCatalogItem, VatRegime


def _spremeni_stanje(model, pk, field, value):
    with transaction.atomic():
        zapis = model.objects.get(pk=pk)
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
        zapis.save(update_fields=[field])
    return StanjeRezultat(True)


def deaktiviraj_ddv_rezim(pk):
    return _spremeni_stanje(VatRegime, pk, "aktiven", False)


def ponovno_aktiviraj_ddv_rezim(pk):
    return _spremeni_stanje(VatRegime, pk, "aktiven", True)


def deaktiviraj_storitev(pk):
    return _spremeni_stanje(ServiceCatalogItem, pk, "aktivna", False)


def ponovno_aktiviraj_storitev(pk):
    return _spremeni_stanje(ServiceCatalogItem, pk, "aktivna", True)
