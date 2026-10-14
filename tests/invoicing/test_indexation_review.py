from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from companies.services import potrdi_ddv_nastavitve
from contracts.indexation import confirm_batch, create_batch
from contracts.models import Lease, LeaseItem
from invoicing.models import MonthlyCycle
from invoicing.services import (
    izracunaj_kandidata,
    izracunaj_mesecne_kandidate,
    potrdi_izjemo_kandidata,
    potrdi_osnutek_np,
    premakni_mesecni_cikel,
    preveri_predhodno_kontrolo,
    pripravi_osnutek_np,
    uskladi_preglede_mesecnega_cikla,
    zamrzni_paket_za_potrditev,
)
from tests.contracts.test_indeksacija import racun_s_postavkami
from tests.invoicing.test_preflight import (  # noqa: F401
    potrdi_septembrski_vzorec,
    ready_company,
)
from tests.setup_helpers import prepare_initial_setup


def approve(data, value="110.000"):
    lease = data["lease"]
    lease.ima_klavzulo_indeksacije = True
    lease.tip_pogoja_indeksacije = Lease.TipPogojaIndeksacije.VEDNO
    lease.save()
    LeaseItem.objects.filter(pk=data["item"].pk).update(
        indeks_ob_osnovi=Decimal("100.000"),
        datum_osnove_indeksacije=date(2026, 1, 1),
    )
    batch = create_batch(data["company"].pk, 2025, Decimal(value))
    confirm_batch(batch.company_id, batch.pk, batch.revision)
    return batch


@pytest.mark.parametrize("value", ["110.000", "100.000"])
def test_first_affected_cycle_requires_individual_confirmation(
    ready_company, potrdi_septembrski_vzorec, value  # noqa: F811
):
    data = ready_company
    approve(data, value)
    potrdi_ddv_nastavitve(data["company"])
    potrdi_septembrski_vzorec(data["company"], data["lease"])
    cycle = data["cycle"]
    preveri_predhodno_kontrolo(cycle)
    premakni_mesecni_cikel(cycle, MonthlyCycle.Korak.IZRACUN_OSNUTKOV)
    izracunaj_mesecne_kandidate(cycle)
    premakni_mesecni_cikel(cycle, MonthlyCycle.Korak.PAKETNI_PREGLED)
    review = uskladi_preglede_mesecnega_cikla(cycle)[0]
    assert review.requires_confirmation
    exception = next(
        row
        for row in review.batch.calculation_snapshot["candidates"][0]["exceptions"]
        if row["code"] == "PRVA_INDEKSACIJA"
    )
    assert "100.00 →" in exception["message"] and "indeks" in exception["message"]
    with pytest.raises(ValidationError):
        zamrzni_paket_za_potrditev(cycle)
    potrdi_izjemo_kandidata(
        cycle,
        lease_id=data["lease"].pk,
        candidate_fingerprint=review.candidate_fingerprint,
    )
    zamrzni_paket_za_potrditev(cycle)
    cycle.refresh_from_db()
    assert cycle.trenutni_korak == MonthlyCycle.Korak.POTRDITEV_PAKETA


def test_january_unaffected_and_review_not_repeated_after_first_issue(ready_company):  # noqa: F811
    data = ready_company
    approve(data)
    potrdi_ddv_nastavitve(data["company"])
    january = izracunaj_kandidata(
        data["lease"], date(2026, 1, 1), date(2026, 1, 31), date(2026, 1, 1)
    )
    assert january.vrstice[0].osnova == Decimal("100.00")
    assert not any(row.koda == "PRVA_INDEKSACIJA" for row in january.izjeme)
    february = izracunaj_kandidata(
        data["lease"], date(2026, 2, 1), date(2026, 2, 28), date(2026, 2, 1)
    )
    assert any(row.koda == "PRVA_INDEKSACIJA" for row in february.izjeme)
    racun_s_postavkami(data["item"])
    march = izracunaj_kandidata(
        data["lease"], date(2026, 3, 1), date(2026, 3, 31), date(2026, 3, 1)
    )
    assert any(row.koda == "PRVA_INDEKSACIJA" for row in march.izjeme)
    prepare_initial_setup(data["lease"].unit.property.company, data["lease"])
    first = potrdi_osnutek_np(pripravi_osnutek_np(
        data["lease"], date(2026, 3, 1), date(2026, 3, 31), date(2026, 3, 1),
    ).invoice)
    line = first.invoiceline_set.get()
    assert line.osnova == Decimal("110.00") and line.ddv_znesek == Decimal("24.20")
    assert line.rent_indexation_event_id is not None
    april = izracunaj_kandidata(
        data["lease"], date(2026, 4, 1), date(2026, 4, 30), date(2026, 4, 1)
    )
    assert not any(row.koda == "PRVA_INDEKSACIJA" for row in april.izjeme)
