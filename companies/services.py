from django.db import transaction
from django.db.models import F

from companies.models import NumberSequence


@transaction.atomic
def dodeli_naslednjo_stevilko(company, tip, datum_izdaje):
    leto = datum_izdaje.year if tip == NumberSequence.Tip.ST else 0
    zaporedje, _ = NumberSequence.objects.get_or_create(
        company=company,
        tip=tip,
        leto=leto,
        defaults={"zadnja_stevilka": 0},
    )
    zaporedje.zadnja_stevilka = F("zadnja_stevilka") + 1
    zaporedje.save(update_fields=["zadnja_stevilka"])
    zaporedje.refresh_from_db(fields=["zadnja_stevilka"])
    return zaporedje.zadnja_stevilka
