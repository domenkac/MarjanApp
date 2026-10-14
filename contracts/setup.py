"""Read-only checks of the selected setup chain using existing validation."""

from calendar import monthrange

from django.core.exceptions import ValidationError
from django.utils.translation import override

from .models import Lease
from .services import hierarhija_pogodbe_je_uporabna, izracunaj_postavko


@override("sl")
def record_setup_errors(record):
    if record is None:
        return ["Izberite ali dodajte zapis."]
    try:
        record.full_clean()
    except ValidationError as error:
        return [
            f"{field.replace('_', ' ')}: {message}"
            for field, messages in error.message_dict.items()
            for message in messages
        ]
    for field in record._meta.fields:
        value = getattr(record, field.name)
        if not field.blank and isinstance(value, str) and not value.strip():
            return [f"Manjka obvezen podatek: {field.verbose_name}."]
    if isinstance(record, Lease) and record.status != Lease.Status.AKTIVNA:
        return ["Izbrana pogodba ni aktivna."]
    return []


def lease_items_setup_errors(lease, today):
    if lease is None:
        return ["Najprej izberite pogodbo."]
    if not hierarhija_pogodbe_je_uporabna(lease):
        return ["Pogodba ali povezan zapis ni aktiven."]
    errors = record_setup_errors(lease)
    if errors:
        return errors
    items = list(
        lease.leaseitem_set.filter(arhivirana=False).select_related("vat_regime")
    )
    if not items:
        return ["Pogodba nima aktivnih pogodbenih postavk."]
    errors = [message for item in items for message in record_setup_errors(item)]
    # Use existing contract calculations without depending on issuer confirmations.
    if not errors:
        try:
            lines = [
                izracunaj_postavko(
                    item,
                    today.replace(day=1),
                    today.replace(day=monthrange(today.year, today.month)[1]),
                )
                for item in items
            ]
            if not any(line is not None for line in lines):
                errors.append("Pogodba nima obračunljivih postavk v tekočem mesecu.")
        except (ValidationError, ValueError) as error:
            errors.append(str(error))
    return errors
